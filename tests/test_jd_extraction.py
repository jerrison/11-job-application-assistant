import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class JdExtractionTests(unittest.TestCase):
    def test_maybe_reexec_with_uv_requests_fetchers_extra(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        with (
            mock.patch.dict("os.environ", {}, clear=False),
            mock.patch.object(scrape_job.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(scrape_job.subprocess, "call", return_value=0) as mock_call,
            mock.patch.object(scrape_job.sys, "argv", ["scripts/scrape_job.py", "https://example.com/job"]),
        ):
            with self.assertRaises(SystemExit) as exc:
                scrape_job.maybe_reexec_with_uv()
        self.assertEqual(exc.exception.code, 0)
        called_cmd = mock_call.call_args.args[0]
        self.assertEqual(called_cmd[:6], ["uv", "run", "--project", str(PROJECT_ROOT), "--extra", "fetchers"][:6])
        self.assertIn("python", called_cmd)

    def test_scrape_job_extracts_jobposting_ld_json(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org/",
                "@type": "JobPosting",
                "title": "Staff Product Manager",
                "description": "<h2>About the role</h2><p>Lead the roadmap for our platform.</p><ul><li>Own prioritization</li><li>Partner with engineering</li></ul>",
                "hiringOrganization": {"name": "Acme"},
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": "US"
                  }
                }
              }
            </script>
          </head>
        </html>
        """
        data = scrape_job.extract_jobposting_ld_json(html, "https://jobs.ashbyhq.com/acme/123")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Staff Product Manager")
        self.assertEqual(data["company"], "Acme")
        self.assertEqual(data["location"], "San Francisco, CA, US")
        self.assertIn("Lead the roadmap", data["full_text"])

    def test_scrape_job_rejects_javascript_shell_text(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        data = {
            "title": "Product Manager | Generalist (All Levels) @ Ramp",
            "company": "",
            "location": "",
            "full_text": "You need to enable JavaScript to run this app.",
        }
        self.assertFalse(scrape_job.is_usable_job_content(data))

    def test_scrape_job_extracts_ashby_app_data(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head><title>Product Manager | Generalist (All Levels) @ Ramp</title></head>
          <body>
            <script>
              window.__appData = {"organization":{"name":"Ramp"},"posting":{"title":"Product Manager | Generalist (All Levels)","locationName":"New York, NY (HQ)","secondaryLocationNames":["San Francisco, CA","Seattle, WA"],"descriptionHtml":"<h2>About the role</h2><p>Build AI-native finance products.</p>"}};
              console.log("loaded");
            </script>
          </body>
        </html>
        """
        data = scrape_job.extract_ashby_app_data(html, "https://jobs.ashbyhq.com/ramp/123")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["company"], "Ramp")
        self.assertEqual(data["location"], "New York, NY (HQ), San Francisco, CA, Seattle, WA")
        self.assertIn("Build AI-native finance products.", data["full_text"])

    def test_scrape_job_extracts_bytedance_search_flight_payload(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = r"""
        <html>
          <head>
            <title>Senior Product Manager (Multiple Positions)</title>
            <meta
              name="description"
              content="View our opening for Senior Product Manager (Multiple Positions) and learn more about what it's like to work at ByteDance!"
            />
          </head>
          <body>
            <div>
              <h2>Senior Product Manager (Multiple Positions)</h2>
              <div>
                <p>Location:</p>
                <p>San Jose</p>
              </div>
              <div>
                <p>Team:</p>
                <p>Product</p>
              </div>
              <div>
                <p>Employment Type:</p>
                <p>Regular</p>
              </div>
              <div>
                <p>Job Code:</p>
                <p>A131568</p>
              </div>
            </div>
            <script>
              self.__next_f.push([1,"About ByteDance\nFounded in 2012, ByteDance's mission is to inspire creativity and enrich life.\n\nWhy Join Us\nInspiring creativity is at the core of ByteDance's mission.\n\nAbout the Team\nOur team plays a crucial role in ensuring the company's success.\n\nResponsibilities\nServe as a product manager for the internal contract system.\nDrive the development and management of role permissions and data dashboard modules.\nMentor junior Product Managers."]);
              self.__next_f.push([1,"Qualifications\nMust have a Master's degree or a Bachelor's degree with progressive related work experience.\nProject management, including collaborating with cross-functional teams to design, develop, and implement data-driven business strategies.\nTranslating data using SQL, Python, Tableau, and Excel.\n\nType: Full time, 40 hours/week\nLocation: San Jose, CA\nSalary Range: $179421 - $311600 per year"]);
            </script>
          </body>
        </html>
        """

        data = scrape_job.extract_structured_html_fallback(
            html,
            "https://joinbytedance.com/search/7613140316427045125",
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Senior Product Manager (Multiple Positions)")
        self.assertEqual(data["company"], "ByteDance")
        self.assertEqual(data["location"], "San Jose")
        self.assertIn("Responsibilities", data["full_text"])
        self.assertIn("Qualifications", data["full_text"])
        self.assertIn("cross-functional teams", data["full_text"])
        self.assertTrue(scrape_job.is_usable_job_content(data))

    def test_scrape_job_extracts_jobvite_structured_html(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head>
            <title>Nutanix Careers - Senior Product Manager- SaaS Marketplace</title>
          </head>
          <body>
            <h2 class="jv-header">Senior Product Manager- SaaS Marketplace</h2>
            <p class="jv-job-detail-meta">
              Engineering
              <span class="jv-inline-separator"></span>
              San Jose, California
              <span class="jv-inline-separator"></span>
              Req.Num.: 31054
            </p>
            <div class="jv-job-detail-description">
              <p><strong>Hungry, Humble, Honest, with Heart.</strong></p>
              <p>
                Nutanix is looking for a Senior Product Manager to lead marketplace, renewals, and billing solutions
                with an AI-first approach. You will define product strategy, partner with engineering and data teams,
                and drive measurable business outcomes across enterprise SaaS workflows.
              </p>
              <ul>
                <li>Own roadmap and prioritization</li>
                <li>Lead AI-enabled workflow delivery</li>
                <li>Partner with cross-functional stakeholders</li>
              </ul>
            </div>
          </body>
        </html>
        """

        data = scrape_job.extract_structured_html_fallback(
            html,
            "https://jobs.jobvite.com/nutanix/job/oWGFzfwA",
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Senior Product Manager- SaaS Marketplace")
        self.assertEqual(data["company"], "Nutanix")
        self.assertEqual(data["location"], "San Jose, California")
        self.assertIn("AI-first approach", data["full_text"])
        self.assertIn("Own roadmap and prioritization", data["full_text"])
        self.assertTrue(scrape_job.is_usable_job_content(data))

    def test_scrape_job_extracts_successfactors_structured_html(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head>
            <meta itemprop="hiringOrganization" content="SAP" />
            <title>Palo Alto Senior Product Manager, Apps AI &amp; Data - SAP Business Network Applications - CA, 94304</title>
          </head>
          <body>
            <div class="jobDisplayShell" itemscope="itemscope" itemtype="http://schema.org/JobPosting">
              <div class="jobDisplay">
                <div class="job">
                  <h1>
                    <span itemprop="title" data-careersite-propertyid="title">
                      Senior Product Manager, Apps AI &amp; Data - SAP Business Network Applications
                    </span>
                  </h1>
                  <span data-careersite-propertyid="description" itemprop="description">
                    <span class="jobdescription">
                      <p><strong>We help the world run better</strong></p>
                      <p>This is a hybrid role based out of Palo Alto.</p>
                      <p>
                        We are seeking a Senior Product Manager for Apps AI &amp; Data to build customer-facing AI
                        capabilities, predictive analytics, agentic workflows, and cross-app intelligence across SAP
                        Business Network applications.
                      </p>
                      <p>
                        In this role, you will collaborate deeply with product leaders across each app to define a
                        cohesive intelligence layer that scales across the entire network and delivers measurable value
                        for buyers, suppliers, carriers, and partners.
                      </p>
                    </span>
                  </span>
                  <span data-careersite-propertyid="location">
                    <p id="job-location" class="jobLocation job-location-inline">Palo Alto, CA, US</p>
                  </span>
                </div>
              </div>
            </div>
          </body>
        </html>
        """

        data = scrape_job.extract_structured_html_fallback(
            html,
            "https://jobs.sap.com/job/Palo-Alto-Senior-Product-Manager%2C-Apps-AI-&-Data-SAP-Business-Network-Applications-CA-94304/1276862301/",
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(
            data["title"],
            "Senior Product Manager, Apps AI & Data - SAP Business Network Applications",
        )
        self.assertEqual(data["company"], "SAP")
        self.assertEqual(data["location"], "Palo Alto, CA, US")
        self.assertIn("agentic workflows", data["full_text"])
        self.assertIn("buyers, suppliers, carriers, and partners", data["full_text"])
        self.assertTrue(scrape_job.is_usable_job_content(data))

    def test_extract_greenhouse_job_data_uses_api_payload(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        payload = {
            "title": "Senior Product Manager",
            "company_name": "Harness",
            "location": {"name": "Remote, US"},
            "content": "<h2>About the role</h2><p>Own the platform roadmap.</p>",
            "departments": [{"name": "Product"}],
        }

        data = scrape_job.extract_greenhouse_job_data(
            payload,
            "https://job-boards.greenhouse.io/harnessinc/jobs/5074953007",
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Senior Product Manager")
        self.assertEqual(data["company"], "Harness")
        self.assertEqual(data["location"], "Remote, US")
        self.assertIn("Own the platform roadmap.", data["full_text"])
        self.assertIn("Department: Product", data["full_text"])

    def test_scrape_job_prefers_greenhouse_api_for_greenhouse_urls(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        direct_url = "https://job-boards.greenhouse.io/harnessinc/jobs/5074953007"
        greenhouse_data = {
            "title": "Senior Product Manager",
            "company": "Harness",
            "location": "Remote, US",
            "full_text": (
                "About the role\n"
                "Lead roadmap decisions across CI/CD and developer platform workflows.\n"
                "Partner with engineering, design, and GTM teams to define strategy, ship customer-facing improvements, "
                "and measure adoption across release orchestration, infrastructure automation, and developer experience.\n"
                "Own prioritization, execution, and communication for a multi-product platform surface used by enterprise teams."
            ),
        }

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=direct_url),
            mock.patch.object(
                scrape_job, "fetch_greenhouse_job_data", return_value=greenhouse_data
            ) as fetch_greenhouse,
            mock.patch.object(scrape_job, "fetch_page") as fetch_page,
        ):
            data, source = scrape_job.scrape_job("https://www.harness.io/company/jobs/apply?gh_jid=5074953007")

        self.assertEqual(source, "greenhouse-api")
        self.assertEqual(data, greenhouse_data)
        fetch_greenhouse.assert_called_once_with(direct_url)
        fetch_page.assert_not_called()

    def test_fetch_greenhouse_job_data_raises_job_closed_when_direct_api_returns_404(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        direct_url = "https://job-boards.greenhouse.io/tripactions/jobs/7616887"
        api_url = "https://boards-api.greenhouse.io/v1/boards/tripactions/jobs/7616887"
        error = scrape_job.HTTPError(api_url, 404, "Not Found", hdrs=None, fp=None)

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=direct_url),
            mock.patch.object(scrape_job, "urlopen", side_effect=error),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "job_closed: Greenhouse posting is no longer available",
            ):
                scrape_job.fetch_greenhouse_job_data(
                    "https://navan.com/careers/openings?gh_jid=7616887&utm_source=trueup.io&utm_medium=website&ref=trueup"
                )

    def test_scrape_job_propagates_terminal_greenhouse_api_errors(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        direct_url = "https://job-boards.greenhouse.io/tripactions/jobs/7616887"

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=direct_url),
            mock.patch.object(
                scrape_job,
                "fetch_greenhouse_job_data",
                side_effect=RuntimeError("job_closed: Greenhouse posting is no longer available."),
            ) as fetch_greenhouse,
            mock.patch.object(
                scrape_job,
                "fetch_page",
                side_effect=AssertionError("fetch_page should not run after a terminal Greenhouse error"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "job_closed: Greenhouse posting is no longer available.",
            ):
                scrape_job.scrape_job(
                    "https://navan.com/careers/openings?gh_jid=7616887&utm_source=trueup.io&utm_medium=website&ref=trueup"
                )

        fetch_greenhouse.assert_called_once_with(direct_url)

    def test_workday_cxs_api_url_omits_locale_segment_for_myworkdayjobs(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        url = (
            "https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/"
            "USA-CA-Pleasanton/Principal-Product-Manager--PATT---Benefits_JR-0104730"
        )

        self.assertEqual(
            scrape_job._workday_cxs_api_url(url),
            "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/job/"
            "USA-CA-Pleasanton/Principal-Product-Manager--PATT---Benefits_JR-0104730",
        )

    def test_company_from_url_uses_rippling_job_board_slug(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        self.assertEqual(
            scrape_job._company_from_url(
                "https://ats.rippling.com/vouch-inc/jobs/255d8d98-dcaf-48a9-94e6-3550fc83982e"
            ),
            "Vouch Inc",
        )

    def test_company_from_url_uses_smartrecruiters_path_slug(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        self.assertEqual(
            scrape_job._company_from_url("https://jobs.smartrecruiters.com/Intuitive/744000098691595"),
            "Intuitive",
        )

    def test_should_try_rendered_browser_for_rippling_and_smartrecruiters(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        self.assertTrue(
            scrape_job._should_try_rendered_browser(
                "https://ats.rippling.com/vouch-inc/jobs/255d8d98-dcaf-48a9-94e6-3550fc83982e",
                "<html></html>",
            )
        )
        self.assertTrue(
            scrape_job._should_try_rendered_browser(
                "https://jobs.smartrecruiters.com/Intuitive/744000098691595",
                "<html></html>",
            )
        )

    def test_should_try_rendered_browser_for_bamboohr(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        self.assertTrue(
            scrape_job._should_try_rendered_browser(
                "https://alkira.bamboohr.com/careers/223",
                "<html></html>",
            )
        )
        self.assertTrue(
            scrape_job._should_try_rendered_browser(
                "https://www.uber.com/global/en/careers/list/155591",
                "",
            )
        )

    def test_should_try_rendered_browser_for_empty_custom_greenhouse_wrapper(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        self.assertTrue(
            scrape_job._should_try_rendered_browser(
                "https://www.weareroku.com/jobs/7687208?gh_jid=7687208&utm_source=trueup.io&utm_medium=website&ref=trueup",
                "",
            )
        )

    def test_extract_rendered_browser_data_strips_smartrecruiters_scaffold_from_title(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        data = scrape_job.extract_rendered_browser_data(
            "https://jobs.smartrecruiters.com/Intuitive/744000098691595",
            "Intuitive Sr Product Manager - Telepresence | SmartRecruiters",
            (
                "Sr Product Manager - Telepresence\n"
                "Full-time\n"
                "Company Description\n"
                "It started with a simple idea: what if surgery could be less invasive and recovery less painful?\n"
                "The Senior Product Manager for Telepresence & Media Capabilities will lead the development, strategy, "
                "and commercialization of telepresence functionality and integrated media solutions.\n"
                "This role sits within the da Vinci Systems Product Management team and will partner closely with "
                "engineering, clinical, regulatory, and commercial teams to define the roadmap and deliver outcomes.\n"
                "The problems we solve demand creativity, rigor, and collaboration, and every improvement we make has "
                "the potential to change a life.\n"
            ),
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Sr Product Manager - Telepresence")
        self.assertEqual(data["company"], "Intuitive")

    def test_extract_rendered_browser_data_parses_bamboohr_public_jd(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        data = scrape_job.extract_rendered_browser_data(
            "https://alkira.bamboohr.com/careers/223",
            "BambooHR",
            (
                "Privacy Policy\n"
                "Job Openings\n"
                "Sr. Product Manager\n"
                "Product - San Jose, California (Hybrid)\n\n"
                "Alkira is reinventing networking for the cloud era and we want to invite you to join us in changing "
                "the industry. We are looking for a highly motivated Senior Product Manager to join our innovative "
                "startup.\n\n"
                "Responsibilities:\n"
                "Ownership of strategy and execution in your area of focus\n"
                "Work closely with Engineering to build the right capabilities\n"
                "Develop collateral, pricing and product positioning\n\n"
                "Requirements:\n"
                "5+ years of experience in product management or technical marketing\n"
                "Must have technical foundation in networking and network services technologies\n\n"
                "About Alkira\n"
                "Alkira was founded in 2018 by Amir and Atif Khan.\n\n"
                "Apply for This Job\n"
                "Link to This Job\n"
                "Location\n"
                "San Jose, California (Hybrid)\n"
                "Department\n"
                "Product\n"
                "Employment Type\n"
                "Full-Time\n"
                "Minimum Experience\n"
                "Experienced\n"
                "Privacy Policy\n"
            ),
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Sr. Product Manager")
        self.assertEqual(data["company"], "Alkira")
        self.assertEqual(data["location"], "San Jose, California (Hybrid)")
        self.assertNotIn("Apply for This Job", data["full_text"])

    def test_resolve_ashby_wrapper_url_uses_embed_resolver(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://jobs.stand.com/careers?ashby_jid=0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7&utm_source=QNwjOoM9DP"
        expected = "https://jobs.ashbyhq.com/standinsurance/0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7?utm_source=QNwjOoM9DP"

        with mock.patch.object(
            job_board_urls,
            "_is_valid_ashby_job_url",
            side_effect=lambda candidate, opener=None: candidate == expected,
        ):
            resolved = job_board_urls.resolve_ashby_wrapper_url(
                url,
                embed_url_resolver=lambda _url: "https://jobs.ashbyhq.com/standinsurance/embed",
            )

        self.assertEqual(resolved, expected)

    def test_is_valid_ashby_job_url_requires_real_posting_payload(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        class FakeResponse:
            def __init__(self, text: str):
                self._text = text

            def read(self):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        shell_html = """
        <html><body><script>
        window.__appData = {"organization": null, "posting": null};
        </script></body></html>
        """
        rich_html = """
        <html><body><script>
        window.__appData = {"organization": {"name": "Handshake"}, "posting": {"title": "Senior Product Manager"}};
        </script></body></html>
        """

        def shell_opener(request, timeout=30):
            return FakeResponse(shell_html)

        def rich_opener(request, timeout=30):
            return FakeResponse(rich_html)

        self.assertFalse(
            job_board_urls._is_valid_ashby_job_url(
                "https://jobs.ashbyhq.com/joinhandshake/3f81effc-91b6-4464-aa9c-9766e7d7c655",
                opener=shell_opener,
            )
        )
        self.assertTrue(
            job_board_urls._is_valid_ashby_job_url(
                "https://jobs.ashbyhq.com/handshake/3f81effc-91b6-4464-aa9c-9766e7d7c655",
                opener=rich_opener,
            )
        )

    def test_resolve_ashby_wrapper_url_rejects_shell_slug_and_uses_embed_slug(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = (
            "https://joinhandshake.com/careers/job/"
            "?ashby_jid=3f81effc-91b6-4464-aa9c-9766e7d7c655&utm_source=4xDZ2XYAXn"
        )
        expected = "https://jobs.ashbyhq.com/handshake/3f81effc-91b6-4464-aa9c-9766e7d7c655?utm_source=4xDZ2XYAXn"

        class FakeResponse:
            def __init__(self, text: str):
                self._text = text

            def read(self):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            if "/joinhandshake/" in request_url:
                return FakeResponse('<script>window.__appData = {"organization": null, "posting": null};</script>')
            if "/handshake/" in request_url:
                return FakeResponse(
                    '<script>window.__appData = {"organization": {"name": "Handshake"}, "posting": {"title": "Senior Product Manager, Operator Experience - Handshake AI"}};</script>'
                )
            raise AssertionError(f"Unexpected URL fetched: {request_url}")

        resolved = job_board_urls.resolve_ashby_wrapper_url(
            url,
            opener=fake_opener,
            embed_url_resolver=lambda _url: "https://jobs.ashbyhq.com/handshake/embed",
        )

        self.assertEqual(resolved, expected)

    def test_resolve_job_source_url_strips_direct_ashby_application_suffix(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.ashbyhq.com/scribe/c3c191f9-847e-413b-87b2-9570274f4967/application?utm_source=LinkedinPaid"
        )

        self.assertEqual(
            resolved,
            "https://jobs.ashbyhq.com/scribe/c3c191f9-847e-413b-87b2-9570274f4967?utm_source=LinkedinPaid",
        )

    def test_resolve_job_source_url_quotes_direct_ashby_paths_with_spaces(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.ashbyhq.com/Hippocratic AI/6f0d4439-25a8-49d7-a0d6-cf535524f471"
        )

        self.assertEqual(
            resolved,
            "https://jobs.ashbyhq.com/Hippocratic%20AI/6f0d4439-25a8-49d7-a0d6-cf535524f471",
        )

    def test_resolve_job_source_url_resolves_greenhouse_wrapper_from_embed_script(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://www.harness.io/company/jobs/apply?gh_jid=5074953007&gh_src=abc123"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    <html>
                      <script src="https://boards.greenhouse.io/embed/job_board/js?for=harnessinc"></script>
                    </html>
                    """,
                )
            if (
                request_url == "https://boards-api.greenhouse.io/v1/boards/harnessinc/jobs/5074953007"
                and method == "HEAD"
            ):
                return FakeResponse(request_url, "", status=200)
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        resolved = job_board_urls.resolve_job_source_url(url, opener=fake_opener)

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/harnessinc/jobs/5074953007",
        )

    def test_resolve_job_source_url_resolves_greenhouse_wrapper_from_local_script(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://www.spotnana.com/careers/careers-listing/?gh_jid=5821424004&gh_src=token123"
        script_url = "https://www.spotnana.com/wp-content/themes/spotnana/js/greenhouse.js"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                return FakeResponse(
                    request_url,
                    f"""
                    <html>
                      <script src="{script_url}"></script>
                    </html>
                    """,
                )
            if request_url == script_url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    window.loadGreenhouse = function () {
                      return "https://boards-api.greenhouse.io/v1/boards/spotnanatechnology/jobs?content=true";
                    };
                    """,
                )
            if (
                request_url == "https://boards-api.greenhouse.io/v1/boards/spotnanatechnology/jobs/5821424004"
                and method == "HEAD"
            ):
                return FakeResponse(request_url, "", status=200)
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        resolved = job_board_urls.resolve_job_source_url(url, opener=fake_opener)

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/spotnanatechnology/jobs/5821424004",
        )

    def test_resolve_job_source_url_resolves_greenhouse_wrapper_from_meta_apply_url(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://careers.veeam.com/job/-/-/22681/92884508496?gh_src=f754f242teu"

        class FakeResponse:
            def __init__(self, url: str, text: str):
                self.url = url
                self._text = text

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            if request_url != url:
                raise AssertionError(f"Unexpected request: {request_url}")
            return FakeResponse(
                request_url,
                """
                <html>
                  <head>
                    <meta
                      name="search-job-apply-url"
                      content="https://job-boards.eu.greenhouse.io/veeamsoftware/jobs/4763795101#application-form"
                    />
                  </head>
                </html>
                """,
            )

        resolved = job_board_urls.resolve_job_source_url(url, opener=fake_opener)

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/veeamsoftware/jobs/4763795101",
        )

    def test_resolve_job_source_url_uses_browser_discovered_greenhouse_job_board_slug_when_static_html_is_thin(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://navan.com/careers/openings/7660273?gh_jid=7660273&gh_src=g19hdlp11us"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    <html>
                      <body>
                        <div id="careers-app">Apply widget mounts client-side.</div>
                      </body>
                    </html>
                    """,
                )
            if request_url == "https://boards-api.greenhouse.io/v1/boards/navan/jobs/7660273" and method == "HEAD":
                raise OSError("404")
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        resolved = job_board_urls.resolve_job_source_url(
            url,
            opener=fake_opener,
            embed_url_resolver=lambda _url: (
                "https://my.greenhouse.io/users/sign_in?job_board=tripactions&source=job_alert_post"
            ),
        )

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/tripactions/jobs/7660273",
        )

    def test_resolve_job_source_url_recovers_from_looping_greenhouse_wrapper_via_base_page_slug_candidates(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://navan.com/careers/openings?gh_jid=7616887&utm_source=trueup.io&utm_medium=website&ref=trueup"
        base_url = "https://navan.com/careers/openings"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                raise OSError("net::ERR_TOO_MANY_REDIRECTS")
            if request_url == base_url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    <html>
                      <body>
                        <img src="https://res.cloudinary.com/tripactions/image/upload/v1/site/footer-badges/example.svg" />
                        <img src="https://res.cloudinary.com/tripactions/image/upload/v1/site/footer-badges/example-2.svg" />
                      </body>
                    </html>
                    """,
                )
            if request_url == "https://boards-api.greenhouse.io/v1/boards/navan/jobs/7616887" and method == "HEAD":
                raise OSError("404")
            if request_url == "https://boards-api.greenhouse.io/v1/boards/tripactions/jobs/7616887" and method == "HEAD":
                return FakeResponse(request_url, "", status=200)
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        resolved = job_board_urls.resolve_job_source_url(
            url,
            opener=fake_opener,
            embed_url_resolver=lambda _url: (_ for _ in ()).throw(
                AssertionError("Looping hosted wrappers should resolve before browser fallback.")
            ),
        )

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/tripactions/jobs/7616887",
        )

    def test_resolve_job_source_url_recovers_looping_greenhouse_wrapper_from_first_party_bundle(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://navan.com/careers/openings?gh_jid=7616887&utm_source=trueup.io&utm_medium=website&ref=trueup"
        base_url = "https://navan.com/careers/openings"
        script_url = "https://navan.com/_next/static/chunks/pages/careers/openings-29e81b171e8115b5.js"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                raise OSError("net::ERR_TOO_MANY_REDIRECTS")
            if request_url == base_url and method == "GET":
                return FakeResponse(
                    request_url,
                    f"""
                    <html>
                      <body>
                        <script src="{script_url}"></script>
                      </body>
                    </html>
                    """,
                )
            if request_url == script_url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    window.__CAREERS_CONFIG__ = {
                      jobsApiUrl: "https://boards-api.greenhouse.io/v1/boards/tripactions/jobs?content=true"
                    };
                    """,
                )
            if request_url == "https://boards-api.greenhouse.io/v1/boards/navan/jobs/7616887" and method == "HEAD":
                raise OSError("404")
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        resolved = job_board_urls.resolve_job_source_url(
            url,
            opener=fake_opener,
            embed_url_resolver=lambda _url: (_ for _ in ()).throw(
                AssertionError("Looping hosted wrappers should resolve from the first-party bundle before browser fallback.")
            ),
        )

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/tripactions/jobs/7616887",
        )

    def test_resolve_job_source_url_recovers_looping_greenhouse_wrapper_from_browser_request_urls(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://navan.com/careers/openings?gh_jid=7616887&utm_source=trueup.io&utm_medium=website&ref=trueup"
        base_url = "https://navan.com/careers/openings"

        class FakeResponse:
            def __init__(self, url: str, text: str, status: int = 200):
                self.url = url
                self._text = text
                self.status = status

            def read(self, _size: int = -1):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            request_url = getattr(request, "full_url", str(request))
            method = getattr(request, "method", None) or request.get_method()
            if request_url == url and method == "GET":
                raise OSError("net::ERR_TOO_MANY_REDIRECTS")
            if request_url == base_url and method == "GET":
                return FakeResponse(
                    request_url,
                    """
                    <html>
                      <body>
                        <div id="careers-app">Apply widget mounts client-side.</div>
                      </body>
                    </html>
                    """,
                )
            if request_url == "https://boards-api.greenhouse.io/v1/boards/navan/jobs/7616887" and method == "HEAD":
                raise OSError("404")
            raise AssertionError(f"Unexpected request: {method} {request_url}")

        class FakeRequest:
            def __init__(self, url: str):
                self.url = url

        class FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.frames = []
                self._handlers: dict[str, object] = {}

            def on(self, event, handler):
                self._handlers[event] = handler

            def goto(self, target, wait_until="domcontentloaded", timeout=30000):
                del wait_until, timeout
                if target == url:
                    raise RuntimeError("Page.goto: net::ERR_TOO_MANY_REDIRECTS")
                self.url = target
                handler = self._handlers.get("request")
                if handler is not None:
                    handler(FakeRequest("https://boards-api.greenhouse.io/v1/boards/tripactions/jobs?content=true"))
                    handler(
                        FakeRequest("https://boards-api.greenhouse.io/v1/boards/tripactions/departments?content=true")
                    )

            def wait_for_timeout(self, _ms):
                pass

            def wait_for_load_state(self, _state, timeout=3000):
                del timeout

            def evaluate(self, _script):
                return []

        class FakeBrowser:
            def __init__(self):
                self.page = FakePage()

            def new_page(self, viewport=None):
                del viewport
                return self.page

            def close(self):
                return None

        class FakePlaywrightContext:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                return False

        with (
            mock.patch.object(job_board_urls, "_load_playwright_sync", return_value=lambda: FakePlaywrightContext()),
            mock.patch.object(job_board_urls, "launch_chromium_browser", return_value=FakeBrowser()),
        ):
            resolved = job_board_urls.resolve_job_source_url(url, opener=fake_opener)

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/tripactions/jobs/7616887",
        )

    def test_resolve_ashby_wrapper_url_falls_back_to_original_wrapper_when_unresolved(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")
        url = "https://redis.io/company/careers/?ashby_jid=1811339d-f89d-4946-b95e-666127aa4f5d&utm_source=qQMzxz6XY3"

        class FakeResponse:
            def __init__(self, text: str):
                self._text = text

            def read(self):
                return self._text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            return FakeResponse('<script>window.__appData = {"organization": null, "posting": null};</script>')

        resolved = job_board_urls.resolve_ashby_wrapper_url(
            url,
            opener=fake_opener,
            embed_url_resolver=lambda _url: None,
        )

        self.assertEqual(resolved, url)

    def test_resolve_job_source_url_strips_lever_apply_suffix_and_query_params(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.lever.co/aircall/6a3fe0a1-94e3-47ac-a945-b7a7183650dc/apply?source=LinkedIn"
        )

        self.assertEqual(
            resolved,
            "https://jobs.lever.co/aircall/6a3fe0a1-94e3-47ac-a945-b7a7183650dc",
        )

    def test_resolve_job_source_url_preserves_lever_job_url_without_apply(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.lever.co/aircall/6a3fe0a1-94e3-47ac-a945-b7a7183650dc"
        )

        self.assertEqual(
            resolved,
            "https://jobs.lever.co/aircall/6a3fe0a1-94e3-47ac-a945-b7a7183650dc",
        )

    def test_resolve_job_source_url_strips_workday_query_params(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/United-States-Boston/Senior-PM_R30990?source=LinkedIn"
        )

        self.assertEqual(
            resolved,
            "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/United-States-Boston/Senior-PM_R30990",
        )

    def test_resolve_job_source_url_preserves_workday_url_without_query(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/United-States-Boston/Senior-PM_R30990"
        )

        self.assertEqual(
            resolved,
            "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/United-States-Boston/Senior-PM_R30990",
        )

    def test_looks_like_non_html_asset_url_detects_static_assets(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        self.assertTrue(
            job_board_urls.looks_like_non_html_asset_url(
                "https://careers.airbnb.com/wp-content/themes/airbnb-careers/favicon.ico"
            )
        )
        self.assertFalse(
            job_board_urls.looks_like_non_html_asset_url("https://careers.airbnb.com/positions/7144534/")
        )

    def test_looks_like_unresolved_url_template_detects_placeholders(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        self.assertTrue(
            job_board_urls.looks_like_unresolved_url_template(
                "https://jobs.jobvite.com/nutanix/job/oWGFzfwA/{{declineUrl}}"
            )
        )
        self.assertTrue(
            job_board_urls.looks_like_unresolved_url_template(
                "https://example.com/jobs/${jobId}/apply"
            )
        )
        self.assertFalse(
            job_board_urls.looks_like_unresolved_url_template(
                "https://jobs.jobvite.com/nutanix/job/oWGFzfwA"
            )
        )

    def test_resolve_job_source_url_canonicalizes_jobvite_placeholder_suffix(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.jobvite.com/nutanix/job/oWGFzfwA/{{declineUrl}}?utm_source=trueup.io"
        )

        self.assertEqual(
            resolved,
            "https://jobs.jobvite.com/nutanix/job/oWGFzfwA",
        )

    def test_resolve_job_source_url_canonicalizes_dover_trailing_slash(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8/?rs=42706078"
        )

        self.assertEqual(
            resolved,
            "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
        )

    def test_resolve_job_source_url_strips_bytedance_tracking_params(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        resolved = job_board_urls.resolve_job_source_url(
            "https://jobs.bytedance.com/en/position/7613140316427045125/detail"
            "?utm_source=trueup.io&utm_medium=website&ref=trueup"
        )

        self.assertEqual(
            resolved,
            "https://jobs.bytedance.com/en/position/7613140316427045125/detail",
        )

    def test_looks_like_phenom_url_global_region(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        # /global/en/job/ path (e.g. McAfee)
        self.assertTrue(
            job_board_urls.looks_like_phenom_url(
                "https://careers.mcafee.com/global/en/job/MCAFGLOBALJR0032313ENGLOBALEXTERNAL/Senior-Director-Product-Analytics"
            )
        )
        # Standard /us/en/job/ still works
        self.assertTrue(job_board_urls.looks_like_phenom_url("https://careers.adobe.com/us/en/job/R12345/PM"))
        # utm_medium=phenom-feeds detection
        self.assertTrue(
            job_board_urls.looks_like_phenom_url(
                "https://careers.mcafee.com/global/en/job/JR123/Role?utm_medium=phenom-feeds"
            )
        )
        # Non-Phenom URL should not match
        self.assertFalse(job_board_urls.looks_like_phenom_url("https://jobs.example.com/careers/role"))

    def test_parse_jd_handles_unicode_section_headers_and_markdown_metadata(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        raw = """
        # Senior Product Manager
        **Company:** Mandolin
        **Location:** San Francisco

        What you’ll do
        - Own roadmap and execution for core AI workflow modules.
        - Partner with clinicians and enterprise stakeholders.

        Must-have experience
        - 5+ years of product management experience.
        - Strong technical fluency across APIs and AI workflows.

        Nice-to-haves
        - Experience with healthcare or life sciences.
        """

        jd_data = parse_jd.parse_jd(raw)

        self.assertEqual(jd_data["company"], "Mandolin")
        self.assertEqual(
            jd_data["responsibilities"],
            [
                "Own roadmap and execution for core AI workflow modules.",
                "Partner with clinicians and enterprise stakeholders.",
            ],
        )
        self.assertEqual(
            jd_data["required_qualifications"],
            [
                "5+ years of product management experience.",
                "Strong technical fluency across APIs and AI workflows.",
            ],
        )
        self.assertEqual(
            jd_data["preferred_qualifications"],
            ["Experience with healthcare or life sciences."],
        )

    def test_parse_jd_handles_some_of_the_things_we_look_for_header(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        raw = """
        # Senior Product Manager - Insights Platform
        Company: Starburst

        About the role
        - Build and deliver on a roadmap that balances the needs of customers and internal teams.
        - Own cross-functional interactions for your features.

        Some of the things we look for:
        - 3+ years experience as a Product Manager or Technical Product Manager.
        - Proven ability to create easily consumable user experiences for complex technical products.
        - Experience with either usage and subscription management or automated telemetry collection.
        """

        jd_data = parse_jd.parse_jd(raw)

        self.assertEqual(
            jd_data["responsibilities"],
            [
                "Build and deliver on a roadmap that balances the needs of customers and internal teams.",
                "Own cross-functional interactions for your features.",
            ],
        )
        self.assertEqual(
            jd_data["required_qualifications"],
            [
                "3+ years experience as a Product Manager or Technical Product Manager.",
                "Proven ability to create easily consumable user experiences for complex technical products.",
                "Experience with either usage and subscription management or automated telemetry collection.",
            ],
        )
        self.assertEqual(jd_data["preferred_qualifications"], [])

    def test_scrape_job_extracts_dover_api_payload(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        payload = {
            "id": "ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8",
            "client_name": "Suppli",
            "title": "Founding Product Manager",
            "user_provided_description": (
                "<h2>About the role</h2><p>Own the roadmap for procurement automation.</p>"
                "<ul><li>Partner with engineering</li><li>Drive AI product discovery</li></ul>"
            ),
            "locations": [
                {"name": "United States"},
                {"name": "Austin, TX"},
            ],
            "compensation": {
                "lower_bound": 170000,
                "upper_bound": 200000,
                "currency_code": "USD",
                "open_to_sharing_comp": True,
                "salary_range_type": "YEARLY",
                "employment_type": "FULL_TIME",
            },
        }

        data = scrape_job.extract_dover_job_data(
            payload,
            "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
        )

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Founding Product Manager")
        self.assertEqual(data["company"], "Suppli")
        self.assertEqual(data["location"], "United States, Austin, TX")
        self.assertIn("Own the roadmap", data["full_text"])
        self.assertIn("Compensation: USD 170,000-200,000 yearly full time", data["full_text"])

    def test_company_from_gem_url_uses_path_tenant(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        company = scrape_job._company_from_url(
            "https://jobs.gem.com/backops-ai/am9icG9zdDqvIJK0_QtZ-fZgDog6VG8z?utm_source=jackandjill"
        )
        self.assertEqual(company, "Backops Ai")

    def test_company_from_dover_url_uses_path_tenant(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        company = scrape_job._company_from_url(
            "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078"
        )
        self.assertEqual(company, "Suppli")

    def test_extract_rendered_browser_data_for_gem_trims_apply_form(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        body_text = """
        View all jobs
        Senior Technical Product Manager
        San Francisco
        About BackOps AI
        BackOps AI is building the AI operating system for logistics teams.
        What you'll do
        Own roadmap prioritization across workflow automation and operator tooling.
        Partner with engineering, design, and go-to-market teams to define product strategy for AI-native back-office operations.
        Translate frontline workflow pain points into product requirements, experiments, and measurable business outcomes.
        Drive customer discovery with logistics operators, dispatchers, and finance teams to validate new automation concepts.
        Build strong execution loops across product delivery, launch readiness, customer feedback, and post-release iteration.
        Ready to apply?
        First name
        Last name
        """
        data = scrape_job.extract_rendered_browser_data(
            "https://jobs.gem.com/backops-ai/am9icG9zdDqvIJK0_QtZ-fZgDog6VG8z",
            "Senior Technical Product Manager",
            body_text,
        )
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["title"], "Senior Technical Product Manager")
        self.assertEqual(data["company"], "BackOps AI")
        self.assertEqual(data["location"], "San Francisco")
        self.assertIn("What you'll do", data["full_text"])
        self.assertNotIn("Ready to apply?", data["full_text"])
        self.assertNotIn("First name", data["full_text"])

    def test_extract_cloudflare_crawl_record_prefers_target_url(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        response = {
            "result": {
                "status": "completed",
                "records": [
                    {
                        "url": "https://example.com/other",
                        "status": "completed",
                        "markdown": "# Other Job\n\nOther content",
                        "metadata": {"title": "Other Job"},
                    },
                    {
                        "url": "https://jobs.ashbyhq.com/ramp/9972df9e-4133-4e2c-9305-49c285b76506",
                        "status": "completed",
                        "markdown": "# Product Manager | Generalist (All Levels)\n\nAbout Ramp\n\nBuild AI-native finance products.",
                        "metadata": {"title": "Product Manager | Generalist (All Levels) · Ramp"},
                    },
                ],
            }
        }
        record = scrape_job.extract_cloudflare_crawl_record(
            response,
            "https://jobs.ashbyhq.com/ramp/9972df9e-4133-4e2c-9305-49c285b76506",
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(
            record["url"],
            "https://jobs.ashbyhq.com/ramp/9972df9e-4133-4e2c-9305-49c285b76506",
        )

    def test_cloudflare_crawl_fallback_uses_first_completed_record_when_url_missing(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        response = {
            "result": {
                "status": "completed",
                "records": [
                    {
                        "status": "running",
                        "markdown": "",
                    },
                    {
                        "status": "completed",
                        "markdown": "# Staff Product Manager\n\nAbout Acme\n\nLead the roadmap and partner with engineering to ship workflows for finance teams.",
                        "metadata": {"title": "Staff Product Manager - Acme"},
                    },
                ],
            }
        }
        record = scrape_job.extract_cloudflare_crawl_record(response, "https://careers.acme.com/jobs/123")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["status"], "completed")

    def test_run_pipeline_validation_rejects_javascript_shell(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = "<!doctype html><html><body>You need to enable JavaScript to run this app.</body></html>"
        jd_data = {
            "title": "Product Manager | Generalist (All Levels) @ Ramp",
            "company": "",
            "location": "",
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "keywords": ["Product", "Manager"],
        }
        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)
        self.assertTrue(any("blocker shell" in issue for issue in issues))

    def test_run_pipeline_validation_rejects_generic_job_openings_title(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        # Job openings

        Careers listing | Spotnana
        Explore our teams, benefits, and open roles around the world.
        """
        jd_data = {
            "title": "Job openings",
            "company": "Spotnana",
            "location": "",
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "keywords": [],
        }

        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)
        self.assertTrue(
            any("credible job title" in issue or issue.startswith("job_closed:") for issue in issues)
        )

    def test_run_pipeline_validation_flags_ashby_null_shell_as_job_closed(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        <html>
          <body>
            <script>
              window.__appData = {"organization": null, "posting": null, "jobBoard": null};
            </script>
          </body>
        </html>
        """
        jd_data = {
            "title": "",
            "company": "",
            "location": "",
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "keywords": [],
        }

        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)

        self.assertTrue(any(issue.startswith("job_closed:") for issue in issues))

    def test_run_pipeline_validation_flags_generic_careers_landing_page_as_job_closed(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        # Make your move at AppFolio

        Open Roles
        Search jobs
        Join our talent community
        Explore benefits, company values, and office locations.
        """
        jd_data = {
            "title": "Make your move at AppFolio",
            "company": "AppFolio",
            "location": "",
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "keywords": ["jobs", "career"],
        }

        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)

        self.assertTrue(any(issue.startswith("job_closed:") for issue in issues))

    def test_scrape_job_flags_large_browse_jobs_listing_as_job_closed(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head><title>My IR - Administration - Career - Browse Jobs</title></head>
          <body>
            <a href="/careers/careers/jobs">Browse Jobs</a>
            <a href="/careers/careers/login/">Sign in or Create an account</a>
            <h4>Search Jobs</h4>
            <label>Job Location</label>
            <label>Job Type</label>
            <article>Principal DevSecOps Engineer - AI Infrastructure</article>
            <article>Machine Learning Engineer - Graph ML & Code Intelligence</article>
            <p>1 - 2 of 2 jobs shown</p>
          </body>
        </html>
        """

        issue = scrape_job._job_unavailable_reason_from_html(
            html,
            "https://ir.elmotalent.com.au/careers/careers/job/view/82",
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertTrue(issue.startswith("job_closed:"))

    def test_scrape_job_flags_hibob_shell_as_unsupported(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head><title>Careers</title></head>
          <body>
            <careers-app-root></careers-app-root>
            <script src="https://front.hibob.com/master-abc/careers/main.js" type="module"></script>
          </body>
        </html>
        """

        issue = scrape_job._job_unavailable_reason_from_html(
            html,
            "https://kiteworks.careers.hibob.com/jobs/f6ee56a2-635b-435c-a4bf-e8c25e4b10cb/apply",
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertTrue(issue.startswith("unsupported:"))

    def test_run_pipeline_validation_flags_large_browse_jobs_listing_as_job_closed(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        My IR - Administration - Career - Browse Jobs

        Browse Jobs
        Sign in or Create an account
        Search Jobs
        Job Location
        Job Type
        Principal DevSecOps Engineer - AI Infrastructure
        Machine Learning Engineer - Graph ML & Code Intelligence
        1 - 2 of 2 jobs shown
        """
        jd_data = {
            "title": "My IR - Administration - Career - Browse Jobs",
            "company": "IR",
            "location": "",
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "keywords": ["jobs", "career"],
        }

        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)

        self.assertTrue(any(issue.startswith("job_closed:") for issue in issues))

    def test_job_unavailable_reason_detects_workday_posting_available_false(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        html = """
        <html>
          <head><title></title></head>
          <body>
            <script>
              window.workday = {
                tenant: "paypal",
                siteId: "jobs",
                postingAvailable: false
              };
            </script>
          </body>
        </html>
        """

        reason = scrape_job._job_unavailable_reason_from_html(
            html,
            "https://paypal.wd1.myworkdayjobs.com/en-US/jobs/job/San-Jose-California-United-States-of-America/Lead-Product-Manager_R0134350-1",
        )

        self.assertEqual(
            reason,
            "job_closed: Workday shell reported postingAvailable=false for this job URL.",
        )

    def test_run_url_extraction_attempt_preserves_job_closed_hint_from_scrape_stderr(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        tmp_dir = PROJECT_ROOT / "tmp" / "test-run-url-extraction-attempt"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_raw = tmp_dir / "jd_raw.md"
        tmp_parsed = tmp_dir / "jd_parsed.json"

        result = mock.Mock(
            returncode=1,
            stdout="",
            stderr="[scrape_job] ERROR: job_closed: URL returned HTTP 404 at https://example.com/jobs/123\n",
        )

        with mock.patch.object(run_pipeline, "_run_step", return_value=result):
            jd_data, issues = run_pipeline._run_url_extraction_attempt(
                label="Scrape JD from URL",
                command=["uv", "run", "python", "scripts/scrape_job.py", "https://example.com/jobs/123"],
                fetcher=None,
                url="https://example.com/jobs/123",
                tmp_raw=tmp_raw,
                tmp_parsed=tmp_parsed,
            )

        self.assertIsNone(jd_data)
        self.assertEqual(issues, ["job_closed: URL returned HTTP 404 at https://example.com/jobs/123"])

    def test_run_url_extraction_attempt_preserves_unsupported_hint_from_scrape_stderr(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        tmp_dir = PROJECT_ROOT / "tmp" / "test-run-url-extraction-attempt-unsupported"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_raw = tmp_dir / "jd_raw.md"
        tmp_parsed = tmp_dir / "jd_parsed.json"

        result = mock.Mock(
            returncode=1,
            stdout="",
            stderr=(
                "[scrape_job] ERROR: unsupported: HiBob-hosted careers pages require dedicated board support and "
                "did not expose a static job description.\n"
            ),
        )

        with mock.patch.object(run_pipeline, "_run_step", return_value=result):
            jd_data, issues = run_pipeline._run_url_extraction_attempt(
                label="Scrape JD from URL",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/scrape_job.py",
                    "https://kiteworks.careers.hibob.com/jobs/f6ee56a2-635b-435c-a4bf-e8c25e4b10cb/apply",
                ],
                fetcher=None,
                url="https://kiteworks.careers.hibob.com/jobs/f6ee56a2-635b-435c-a4bf-e8c25e4b10cb/apply",
                tmp_raw=tmp_raw,
                tmp_parsed=tmp_parsed,
            )

        self.assertIsNone(jd_data)
        self.assertEqual(
            issues,
            [
                "unsupported: HiBob-hosted careers pages require dedicated board support and "
                "did not expose a static job description."
            ],
        )

    def test_run_pipeline_uses_precanonical_board_url_for_extraction_attempts(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        source_url = "https://careers-jobyaviation.icims.com/jobs/4622/job?utm_source=trueup.io&utm_medium=website"
        canonical_url = "https://careers-jobyaviation.icims.com/jobs/4622"
        tmp_dir = PROJECT_ROOT / "tmp" / "test-run-pipeline-extraction-url"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        captured_urls: list[str] = []

        class StopAfterCapture(Exception):
            pass

        def fake_run_url_extraction_attempt(**kwargs):
            captured_urls.append(kwargs["url"])
            raise StopAfterCapture

        with (
            mock.patch.object(run_pipeline.sys, "argv", ["scripts/run_pipeline.py", source_url, "--skip-sync"]),
            mock.patch.object(run_pipeline, "detect_source", return_value="direct"),
            mock.patch.object(run_pipeline, "resolve_job_source_url", return_value=canonical_url),
            mock.patch.object(run_pipeline, "looks_like_non_html_asset_url", return_value=False),
            mock.patch.object(run_pipeline, "looks_like_unresolved_url_template", return_value=False),
            mock.patch.object(run_pipeline, "looks_like_greenhouse_url", return_value=False),
            mock.patch.object(run_pipeline, "_url_prefers_stealth", return_value=False),
            mock.patch.object(run_pipeline, "_create_pipeline_tmp_dir", return_value=tmp_dir),
            mock.patch.object(run_pipeline, "_enforce_domain_rate_limit", return_value=None),
            mock.patch.object(run_pipeline, "_run_url_extraction_attempt", side_effect=fake_run_url_extraction_attempt),
        ):
            with self.assertRaises(StopAfterCapture):
                run_pipeline.main()

        self.assertEqual(captured_urls, [source_url])

    def test_fetch_raw_html_raises_skipped_captcha_for_access_denied_403(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        url = "https://www.tesla.com/careers/search/job/251870"
        body = """
        <html><head><title>Access Denied</title></head><body>
        <h1>Access Denied</h1>
        You don't have permission to access this resource on this server.
        <p>https://errors.edgesuite.net/example</p>
        </body></html>
        """
        http_error = HTTPError(
            url,
            403,
            "Forbidden",
            {"server": "AkamaiGHost", "content-type": "text/html"},
            io.BytesIO(body.encode("utf-8")),
        )

        with mock.patch.object(scrape_job, "urlopen", side_effect=http_error):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^skipped_captcha: .*anti-bot challenge.*https://www\.tesla\.com/careers/search/job/251870",
            ):
                scrape_job.fetch_raw_html(url)

    def test_run_url_extraction_attempt_preserves_skipped_captcha_hint_from_scrape_stderr(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        tmp_dir = PROJECT_ROOT / "tmp" / "test-run-url-extraction-attempt-skipped-captcha"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_raw = tmp_dir / "jd_raw.md"
        tmp_parsed = tmp_dir / "jd_parsed.json"

        result = mock.Mock(
            returncode=1,
            stdout="",
            stderr=(
                "[scrape_job] ERROR: skipped_captcha: The job board blocked access to the job description behind an "
                "anti-bot challenge at https://www.tesla.com/careers/search/job/251870\n"
            ),
        )

        with mock.patch.object(run_pipeline, "_run_step", return_value=result):
            jd_data, issues = run_pipeline._run_url_extraction_attempt(
                label="Scrape JD from URL",
                command=["uv", "run", "python", "scripts/scrape_job.py", "https://www.tesla.com/careers/search/job/251870"],
                fetcher=None,
                url="https://www.tesla.com/careers/search/job/251870",
                tmp_raw=tmp_raw,
                tmp_parsed=tmp_parsed,
            )

        self.assertIsNone(jd_data)
        self.assertEqual(
            issues,
            [
                "skipped_captcha: The job board blocked access to the job description behind an anti-bot challenge at https://www.tesla.com/careers/search/job/251870"
            ],
        )

    def test_terminal_url_extraction_issue_detects_learn_about_jobvite_landing_page(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        # Learn About Nutanix

        Open Positions
        Search Jobs
        Job listing
        Job location
        Systems Sales Engineer
        3 Locations
        Manager, Systems Engineering
        2 Locations
        """

        issue = run_pipeline._terminal_url_extraction_issue(
            raw,
            {"title": "Learn About Nutanix"},
            normalized=raw,
        )

        self.assertEqual(
            issue,
            "job_closed: The extracted page resolved to a generic careers landing page instead of a specific job posting.",
        )

    def test_terminal_url_extraction_issue_detects_promotional_landing_page_with_slogan_title(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        # What if You Could?

        What if You Could?
        You deserve more, and then some.
        Explore what's possible at LPL Financial.
        Advisors
        Institutions
        Individual Investors
        Find an Advisor
        Leadership, Size, and Strength
        Industry Insights
        Weekly Market Commentary
        Disclosures
        Paid Advertisement
        """

        issue = run_pipeline._terminal_url_extraction_issue(
            raw,
            {"title": "What if You Could?"},
            normalized=raw,
        )

        self.assertEqual(
            issue,
            "job_closed: The extracted page resolved to a generic careers landing page instead of a specific job posting.",
        )

    def test_scrape_job_marks_promotional_landing_page_with_slogan_title_as_job_closed(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        reason = scrape_job._job_unavailable_reason_from_extracted_data(
            "http://www.lpl.com/",
            {
                "title": "What if You Could?",
                "full_text": """
                What if You Could?
                You deserve more, and then some.
                Explore what's possible at LPL Financial.
                Advisors
                Institutions
                Individual Investors
                Find an Advisor
                Leadership, Size, and Strength
                Industry Insights
                Weekly Market Commentary
                Disclosures
                Paid Advertisement
                """,
            },
        )

        self.assertEqual(
            reason,
            "job_closed: The extracted page resolved to a generic careers landing page instead of a specific job posting.",
        )

    def test_scrape_job_raises_job_closed_from_scrapling_page_not_found(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value="https://www.uber.com/global/en/careers/list/155591"),
            mock.patch.object(scrape_job, "fetch_page", return_value=object()),
            mock.patch.object(
                scrape_job,
                "extract_job_content",
                return_value={
                    "title": "Page Not Found | Uber",
                    "company": "Uber",
                    "location": "",
                    "full_text": "Page Not Found\nThe page you requested could not be found.\nBrowse jobs",
                },
            ),
            mock.patch.object(
                scrape_job,
                "fetch_raw_html",
                side_effect=HTTPError(
                    "https://www.uber.com/global/en/careers/list/155591",
                    406,
                    "Not Acceptable",
                    {},
                    None,
                ),
            ),
            mock.patch.object(scrape_job, "_should_try_rendered_browser", return_value=False),
            mock.patch.object(scrape_job, "extract_cloudflare_crawl_fallback", return_value=None),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^job_closed: The extracted page explicitly says the posting is unavailable\.",
            ):
                scrape_job.scrape_job("https://www.uber.com/global/en/careers/list/155591")

    def test_parse_jd_skips_careers_at_wrapper_heading_and_uses_real_role_title(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        raw = """
        # CAREERS AT NVIDIA

        Senior Product Manager - Enterprise Networking Services

        NVIDIA is looking for a product leader to define and launch enterprise networking services.

        What you'll do
        - Drive roadmap strategy for networking service capabilities
        - Partner with engineering and GTM teams to deliver enterprise outcomes
        """

        data = parse_jd.parse_jd(raw)

        self.assertEqual(data["title"], "Senior Product Manager - Enterprise Networking Services")
        self.assertEqual(data["company"], "NVIDIA")

    def test_run_pipeline_validation_accepts_real_jd_text(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """
        # Staff Product Manager

        About Acme
        Acme builds finance infrastructure for modern businesses.

        About the role
        You will define roadmap and strategy for our platform products.

        What you'll do
        - Lead roadmap prioritization across payments and procurement
        - Partner with engineering and design to ship customer-facing workflows
        - Define metrics and run experiments to improve adoption

        What you'll need
        - 5+ years of product management experience
        - Strong SQL and analytics skills
        - Experience building B2B SaaS or fintech products
        """
        jd_data = parse_jd.parse_jd(raw)
        issues = run_pipeline._validate_url_jd_extraction(raw, jd_data)
        self.assertEqual(issues, [])

    def test_scrape_job_searches_same_site_when_ashby_page_is_thin(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        original_url = "https://careers.mandolin.com/jobs?ashby_jid=84421bfb-dc71-4838-a518-f5b209d69d9a"
        resolved_url = "https://jobs.ashbyhq.com/mandolin/84421bfb-dc71-4838-a518-f5b209d69d9a"
        same_site_jd_url = "https://careers.mandolin.com/jobs/senior-product-manager"
        thin_ashby_html = """
        <html>
          <head><title>Apply</title></head>
          <body>
            <script>window.__appData = {"organization":{"name":"Mandolin"},"posting":{"title":"Senior Product Manager","descriptionHtml":""}};</script>
          </body>
        </html>
        """
        wrapper_html = """
        <html>
          <head><title>Mandolin Careers</title></head>
          <body>
            <a href="/jobs/senior-product-manager">Senior Product Manager</a>
          </body>
        </html>
        """
        rich_same_site_html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org/",
                "@type": "JobPosting",
                "title": "Senior Product Manager",
                "description": "<h2>What you'll do</h2><p>Own roadmap and strategy for AI workflow automation across complex healthcare operations.</p><ul><li>Partner with engineering and clinicians to design reliable customer-facing workflows.</li><li>Translate user pain points into requirements, roadmap priorities, and measurable adoption goals.</li><li>Lead launch planning, iteration, and cross-functional execution with design and deployment teams.</li></ul>",
                "hiringOrganization": {"name": "Mandolin"},
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": "US"
                  }
                }
              }
            </script>
          </head>
        </html>
        """

        def fake_fetch_raw_html(url: str) -> str:
            payloads = {
                resolved_url: thin_ashby_html,
                original_url: wrapper_html,
                same_site_jd_url: rich_same_site_html,
            }
            return payloads[url]

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=resolved_url),
            mock.patch.object(
                scrape_job,
                "fetch_page",
                return_value=object(),
            ),
            mock.patch.object(
                scrape_job,
                "extract_job_content",
                return_value={
                    "title": "Senior Product Manager",
                    "company": "Mandolin",
                    "location": "",
                    "full_text": "Apply now",
                },
            ),
            mock.patch.object(
                scrape_job,
                "fetch_raw_html",
                side_effect=fake_fetch_raw_html,
            ),
            mock.patch.object(
                scrape_job,
                "_should_try_rendered_browser",
                return_value=False,
            ),
            mock.patch.object(
                scrape_job,
                "extract_cloudflare_crawl_fallback",
                return_value=None,
            ),
        ):
            data, source = scrape_job.scrape_job(original_url)

        self.assertEqual(source, "same-site-search")
        self.assertEqual(data["company"], "Mandolin")
        self.assertEqual(data["title"], "Senior Product Manager")
        self.assertIn("Own roadmap and strategy", data["full_text"])

    def test_scrape_job_searches_same_site_when_canonical_page_exposes_job_iframe(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        canonical_url = "https://careers-jobyaviation.icims.com/jobs/4622"
        iframe_job_url = "https://careers-jobyaviation.icims.com/jobs/4622/job?in_iframe=1"
        thin_canonical_html = """
        <html>
          <head><title>Apply</title></head>
          <body>
            <iframe src="/jobs/4622?in_iframe=1"></iframe>
          </body>
        </html>
        """
        rich_iframe_html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org/",
                "@type": "JobPosting",
                "title": "Product Management Lead",
                "description": "<h2>Company Overview</h2><p>Lead factory systems product strategy for aircraft manufacturing, certification readiness, and launch planning across a highly regulated aerospace environment.</p><p>You will define roadmap priorities, align software and operations teams, and drive measurable improvements in production throughput, traceability, and quality workflows.</p><h2>Responsibilities</h2><ul><li>Own roadmap and execution across manufacturing systems, analytics, and internal platforms.</li><li>Partner with engineering, quality, supply chain, and operations leaders to define requirements and deliver cross-functional programs.</li><li>Translate certification and production constraints into scalable product decisions, rollout plans, and measurable adoption goals.</li></ul>",
                "hiringOrganization": {"name": "Joby Aviation"},
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "San Carlos",
                    "addressRegion": "CA",
                    "addressCountry": "US"
                  }
                }
              }
            </script>
          </head>
        </html>
        """

        def fake_fetch_raw_html(url: str) -> str:
            payloads = {
                canonical_url: thin_canonical_html,
                iframe_job_url: rich_iframe_html,
            }
            return payloads[url]

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=canonical_url),
            mock.patch.object(scrape_job, "fetch_page", return_value=object()),
            mock.patch.object(
                scrape_job,
                "extract_job_content",
                return_value={
                    "title": "Apply",
                    "company": "Joby Aviation",
                    "location": "",
                    "full_text": "Continue your application",
                },
            ),
            mock.patch.object(scrape_job, "fetch_raw_html", side_effect=fake_fetch_raw_html),
            mock.patch.object(scrape_job, "_should_try_rendered_browser", return_value=False),
            mock.patch.object(scrape_job, "extract_cloudflare_crawl_fallback", return_value=None),
        ):
            data, source = scrape_job.scrape_job(canonical_url)

        self.assertEqual(source, "icims-html")
        self.assertEqual(data["company"], "Joby Aviation")
        self.assertEqual(data["title"], "Product Management Lead")
        self.assertIn("Lead factory systems product strategy", data["full_text"])

    def test_scrape_job_uses_icims_job_iframe_fallback_when_wrapper_and_intro_shell_are_thin(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        original_url = (
            "https://idccareers-idg.icims.com/jobs/6328/principal-product-manager---technical/"
            "job?mode=apply&iis=LinkedIn&iisn=LinkedIn"
        )
        canonical_url = "https://idccareers-idg.icims.com/jobs/6328/principal-product-manager---technical"
        job_iframe_url = f"{canonical_url}/job?in_iframe=1"
        thin_wrapper_html = """
        <html>
          <head><title>IDC - Careers</title></head>
          <body>
            <iframe src="/jobs/6328/principal-product-manager---technical?in_iframe=1"></iframe>
          </body>
        </html>
        """
        thin_intro_html = """
        <html>
          <head><title>International Data Group | Careers Center | Welcome</title></head>
          <body>
            <div class="iCIMS_ErrorMsgTitle">Please Enable Cookies to Continue</div>
            <h1>Enter Your Information</h1>
          </body>
        </html>
        """
        rich_job_html = """
        <html>
          <head>
            <title>Principal Product Manager - Technical in  | Careers at United States - Remote</title>
            <script type="application/ld+json">
              {
                "@context": "http://schema.org",
                "@type": "JobPosting",
                "title": "Principal Product Manager - Technical",
                "description": "<h2>Overview</h2><p>IDC is building the next generation of intelligent, AI-powered platforms that transform how technology decisions get made.</p><p>We are looking for a remote Principal Product Manager - Technical to lead product strategy, architecture, and execution for a core pillar of this platform.</p><h2>What You'll Do</h2><ul><li>Own the product vision, strategy, and roadmap.</li><li>Define data models, technical architecture, and platform interfaces.</li><li>Partner with engineering and AI/ML teams to deliver scalable APIs and intelligent product experiences.</li></ul>",
                "hiringOrganization": {"@type": "Organization", "name": "IDC Research Inc."},
                "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Remote - East Coast", "addressCountry": "US"}}],
                "url": "https://idccareers-idg.icims.com/jobs/6328/principal-product-manager---technical/job"
              }
            </script>
          </head>
        </html>
        """

        def fake_fetch_raw_html(url: str) -> str:
            payloads = {
                canonical_url: thin_wrapper_html,
                f"{canonical_url}?in_iframe=1": thin_intro_html,
                job_iframe_url: rich_job_html,
            }
            return payloads[url]

        with (
            mock.patch.object(scrape_job, "resolve_job_source_url", return_value=canonical_url),
            mock.patch.object(scrape_job, "fetch_page", return_value=object()),
            mock.patch.object(
                scrape_job,
                "extract_job_content",
                return_value={
                    "title": "IDC - Careers",
                    "company": "IDC",
                    "location": "",
                    "full_text": "Join our talent community and browse careers.",
                },
            ),
            mock.patch.object(scrape_job, "fetch_raw_html", side_effect=fake_fetch_raw_html),
            mock.patch.object(scrape_job, "_should_try_rendered_browser", return_value=False),
            mock.patch.object(scrape_job, "extract_cloudflare_crawl_fallback", return_value=None),
        ):
            data, source = scrape_job.scrape_job(original_url)

        self.assertEqual(source, "icims-html")
        self.assertEqual(data["company"], "IDC Research Inc.")
        self.assertEqual(data["title"], "Principal Product Manager - Technical")
        self.assertIn("Define data models, technical architecture, and platform interfaces", data["full_text"])


if __name__ == "__main__":
    unittest.main()
