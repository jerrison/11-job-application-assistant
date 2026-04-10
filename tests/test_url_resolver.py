import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from url_resolver import (
    _ensure_linkedin_logged_in,
    _extract_linkedin_job_id,
    _resolve_company_url_to_board,
    detect_source,
    resolve_to_board_url,
)


def test_detect_linkedin():
    assert detect_source("https://www.linkedin.com/jobs/view/12345") == "linkedin"


def test_detect_indeed():
    assert detect_source("https://www.indeed.com/viewjob?jk=abc123") == "indeed"


def test_detect_glassdoor():
    assert detect_source("https://www.glassdoor.com/job-listing/pm-j123.htm") == "glassdoor"


def test_detect_dice():
    assert detect_source("https://www.dice.com/job-detail/abc123") == "dice"


def test_detect_ziprecruiter():
    assert detect_source("https://www.ziprecruiter.com/job/123") == "ziprecruiter"


def test_detect_trueup():
    assert detect_source("https://www.trueup.io/jobs/acme-senior-product-manager-123") == "trueup"


def test_detect_breezy_direct():
    assert detect_source("https://zero-hash.breezy.hr/p/7801647b617f-role") == "direct"


def test_detect_successfactors_direct():
    assert detect_source("https://career4.successfactors.com/career?company=supermicro") == "direct"


def test_detect_jobvite_direct():
    assert detect_source("https://jobs.jobvite.com/garten/job/oiiKcfwg") == "direct"


def test_detect_jazzhr_direct():
    assert detect_source("https://jobs.applytojob.com/apply/12345/senior-product-manager") == "direct"


def test_detect_successfactors_marketing_is_unknown():
    assert detect_source("https://www.successfactors.com/") == "unknown"


def test_detect_breezy_marketing_is_unknown():
    assert detect_source("https://www.breezy.hr/") == "unknown"


def test_detect_recruitee_marketing_is_unknown():
    assert detect_source("https://www.recruitee.com/") == "unknown"


def test_detect_jobvite_marketing_is_unknown():
    assert detect_source("https://www.jobvite.com/") == "unknown"


def test_detect_paycor_direct():
    assert detect_source("https://recruitingbypaycor.com/Recruiting/Jobs/1234") == "direct"


def test_detect_jackandjill():
    assert (
        detect_source("https://app.jackandjill.ai/jack/dashboard/jobs/opportunities")
        == "jackandjill"
    )


def test_detect_greenhouse_direct():
    assert detect_source("https://boards.greenhouse.io/company/jobs/123") == "direct"


def test_detect_phenom_direct():
    assert detect_source("https://careers.adobe.com/us/en/job/R12345") == "direct"


def test_detect_bytedance_direct():
    assert (
        detect_source(
            "https://jobs.bytedance.com/en/position/7613140316427045125/detail"
            "?utm_source=trueup.io&utm_medium=website&ref=trueup"
        )
        == "direct"
    )


def test_detect_unknown():
    assert detect_source("https://some-random-site.com/jobs/123") == "unknown"


def test_detect_easyapply():
    assert detect_source("https://easyapply.jobs/r/UEES9hU25Z0x8U4j1bie") == "easyapply"


def test_extract_linkedin_job_id_from_view_path():
    assert _extract_linkedin_job_id("https://www.linkedin.com/jobs/view/12345/?refId=abc") == "12345"


def test_extract_linkedin_job_id_from_current_job_id_query():
    assert _extract_linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=67890") == "67890"


def test_ensure_linkedin_logged_in_falls_back_to_shared_login_email(monkeypatch):
    class FakeLocator:
        def __init__(self):
            self.filled: list[str] = []
            self.clicked = 0

        @property
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
            self.url = "https://www.linkedin.com/login"
            self.email = FakeLocator()
            self.password = FakeLocator()
            self.submit = FakeLocator()

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _timeout):
            self.url = "https://www.linkedin.com/feed/"

        def locator(self, selector):
            if "session_key" in selector or "#username" in selector:
                return self.email
            if "session_password" in selector or "#password" in selector:
                return self.password
            if "button" in selector:
                return self.submit
            raise AssertionError(f"Unexpected selector: {selector}")

    monkeypatch.setenv("JOB_ASSETS_LOGIN_EMAIL", "shared@example.test")
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.setenv("LINKEDIN_PASSWORD", "linkedin-secret")

    page = FakePage()

    assert _ensure_linkedin_logged_in(page) is True
    assert page.email.filled == ["shared@example.test"]
    assert page.password.filled == ["linkedin-secret"]
    assert page.submit.clicked == 1


def test_resolve_to_board_url_returns_redirected_jobish_url_for_easyapply():
    class FakeResponse:
        url = "https://kiteworks.careers.hibob.com/jobs/f6ee56a2-1b95-48c3-96ce-4fdcb47917b5/apply"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        resolved = resolve_to_board_url("https://easyapply.jobs/r/UEES9hU25Z0x8U4j1bie")

    assert (
        resolved
        == "https://kiteworks.careers.hibob.com/jobs/f6ee56a2-1b95-48c3-96ce-4fdcb47917b5/apply"
    )


def test_resolve_to_board_url_canonicalizes_jobvite_placeholder_redirect():
    class FakeResponse:
        url = "https://jobs.jobvite.com/nutanix/job/oWGFzfwA/{{declineUrl}}"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        resolved = resolve_to_board_url("https://www.trueup.io/jobs/nutanix-senior-product-manager-123")

    assert resolved == "https://jobs.jobvite.com/nutanix/job/oWGFzfwA"


def test_resolve_company_url_to_board_uses_json_applyurl_probe():
    class FakeResponse:
        url = "https://careers.usbank.com/global/en/job/UBNAGLOBAL20260004659EXTERNALENGLOBAL/Senior-AI-Platform-Product-Manager"

        def read(self, _size: int = -1):
            return b"""
            <html>
              <body>
                <script>
                  window.phApp = {
                    "jobDetail": {
                      "data": {
                        "job": {
                          "externalApply": true,
                          "applyUrl": "https://usbank.wd1.myworkdayjobs.com/US_Bank_Careers/job/Chicago-IL/AI-Platform-Product-Manager_2026-0004659/apply"
                        }
                      }
                    }
                  };
                </script>
              </body>
            </html>
            """

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        resolved = _resolve_company_url_to_board(
            "https://careers.usbank.com/global/en/job/UBNAGLOBAL20260004659EXTERNALENGLOBAL/Senior-AI-Platform-Product-Manager"
        )

    assert (
        resolved
        == "https://usbank.wd1.myworkdayjobs.com/US_Bank_Careers/job/Chicago-IL/AI-Platform-Product-Manager_2026-0004659/apply"
    )
