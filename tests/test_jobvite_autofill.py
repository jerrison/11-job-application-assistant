import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_jobvite_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_for_url_detects_jobvite_direct_host():
    url = "https://jobs.jobvite.com/garten/job/oiiKcfwg"
    assert looks_like_jobvite_url(url)
    assert _board_for_url(url) == "jobvite"


def test_board_payload_uses_jobvite_name(tmp_path):
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")
    out_dir = tmp_path / "role"
    out_dir.mkdir()
    resume_path = out_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-fake")

    with (
        mock.patch.object(autofill, "migrate_role_output_layout"),
        mock.patch.object(
            autofill,
            "load_meta",
            return_value={
                "jd_source": "https://jobs.jobvite.com/garten/job/oiiKcfwg",
                "jd_source_resolved": "https://www.garten.com/favicon.ico",
                "company": "garten",
                "company_proper": "Garten",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Jerrison",
                last_name="Li",
                email="jerrison@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                country="United States",
                location="San Francisco, CA",
                how_did_you_hear="Corporate website",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
                gender="Male",
                education_entries=[
                    "The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)"
                ],
                education_graduation_month_years=["05/2020"],
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
        mock.patch.object(autofill, "primary_employer_name", return_value="Moody's Analytics"),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "jobvite"
    assert payload["job_url"] == "https://jobs.jobvite.com/garten/job/oiiKcfwg"
    parsed_application_url = urlparse(payload["application_url"])
    assert parsed_application_url.scheme == "https"
    assert parsed_application_url.netloc == "jobs.jobvite.com"
    assert parsed_application_url.path == "/garten/job/oiiKcfwg/apply"
    assert parsed_application_url.query == ""
    steps_by_field = {step["field_name"]: step for step in payload["steps"]}
    assert steps_by_field["company"]["value"] == "Moody's Analytics"
    assert steps_by_field["country"]["value"] == "United States"
    assert steps_by_field["how_did_you_hear"]["value"] == "Other - please list source below"
    assert steps_by_field["additional_info"]["value"] == "Corporate website"
    assert steps_by_field["school_name"]["value"] == "The Wharton School, University of Pennsylvania"
    assert steps_by_field["graduation_month_year"]["value"] == "05/2020"
    assert steps_by_field["highest_level_of_qualification"]["value"] == "Master's Degree"
    assert steps_by_field["gender"]["value"] == "Male"


def test_jobvite_submit_state_detects_confirmation():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    state = autofill._classify_submit_state(
        {
            "page_text": "Thank you for your interest",
            "url": "https://jobs.jobvite.com/company/job/abc",
            "errors": [],
            "invalid_fields": [],
            "recaptcha_visible": False,
            "recaptcha_challenge_active": False,
        }
    )
    assert state["status"] == "confirmed"


def test_jobvite_payload_includes_second_page_self_id_steps(tmp_path):
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")
    out_dir = tmp_path / "role"
    out_dir.mkdir()
    resume_path = out_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-fake")

    with (
        mock.patch.object(autofill, "migrate_role_output_layout"),
        mock.patch.object(
            autofill,
            "load_meta",
            return_value={
                "jd_source": "https://jobs.jobvite.com/nutanix/job/otNPzfwo",
                "jd_source_resolved": "https://jobs.jobvite.com/nutanix/job/otNPzfwo",
                "company": "nutanix",
                "company_proper": "Nutanix",
                "jd_title": "Senior Product Manager - AI Analytics & Adoption",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Jerrison",
                last_name="Li",
                email="jerrison@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                country="United States",
                location="San Francisco, CA",
                how_did_you_hear="Corporate website",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
                gender="Male",
                race_or_ethnicity="Hispanic or Latino",
                veteran_status="I am not a protected veteran",
                disability_status="No, I do not have a disability and have not had one in the past",
                education_entries=[
                    "The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)"
                ],
                education_graduation_month_years=["05/2020"],
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
        mock.patch.object(autofill, "primary_employer_name", return_value="Moody's Analytics"),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
        mock.patch.object(autofill, "_jobvite_today_iso", return_value="2026-04-07"),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    steps_by_field = {step["field_name"]: step for step in payload["steps"]}
    expected_page_two_fields = {
        "f3": ("Male", "gender"),
        "f5": ("Hispanic or Latino", "race_or_ethnicity"),
        "f7": ("Jerrison Li", None),
        "date8": ("2026-04-07", None),
        "f18": ("I am not a protected veteran", "veteran_status"),
        "f20": ("Jerrison Li", None),
        "date21": ("2026-04-07", None),
        "f33": ("No, I do not have a disability and have not had one in the past", "disability_status"),
        "f35": ("Jerrison Li", None),
        "date36": ("2026-04-07", None),
    }

    for field_name, (value, profile_field) in expected_page_two_fields.items():
        assert steps_by_field[field_name]["page_index"] == 2
        assert steps_by_field[field_name]["value"] == value
        assert steps_by_field[field_name]["required"] is True
        if profile_field is None:
            assert "profile_field" not in steps_by_field[field_name]
        else:
            assert steps_by_field[field_name]["profile_field"] == profile_field


def test_jobvite_fill_step_advances_once_before_second_page_fields():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    filled: list[tuple[int, str]] = []
    page = object()
    advance_calls: list[object] = []

    def fake_fill_step(active_page, step):
        assert active_page is page
        current_page = len(advance_calls) + 1
        filled.append((current_page, step["field_name"]))
        step["filled"] = True

    with mock.patch.object(
        autofill,
        "_advance_jobvite_page",
        side_effect=lambda active_page: advance_calls.append(active_page) or True,
    ):
        fill_step = autofill._build_fill_step_fn(base_fill_step_fn=fake_fill_step)
        fill_step(page, {"field_name": "first_name"})
        fill_step(page, {"field_name": "f5", "page_index": 2})
        fill_step(page, {"field_name": "f7", "page_index": 2})

    assert advance_calls == [page]
    assert filled == [(1, "first_name"), (2, "f5"), (2, "f7")]


def test_jobvite_fill_step_skips_later_page_two_fields_after_failed_page_advance():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    page = object()
    advance_calls: list[object] = []

    def fake_fill_step(_active_page, step):
        raise AssertionError(f"page-two field should not fill after failed advance: {step['field_name']}")

    with mock.patch.object(
        autofill,
        "_advance_jobvite_page",
        side_effect=lambda active_page: advance_calls.append(active_page) or False,
    ):
        fill_step = autofill._build_fill_step_fn(base_fill_step_fn=fake_fill_step)
        first_page_two = {"field_name": "f5", "label": "Race or Ethnicity", "page_index": 2}
        second_page_two = {"field_name": "f7", "label": "Your Name", "page_index": 2}
        fill_step(page, first_page_two)
        fill_step(page, second_page_two)

    assert advance_calls == [page]
    assert first_page_two["status"] == "skipped_not_found"
    assert "Could not advance Jobvite form to page 2" in first_page_two["note"]
    assert second_page_two["status"] == "skipped_not_found"
    assert "could not advance to page 2" in second_page_two["note"].casefold()


def test_jobvite_post_navigate_hook_advances_location_and_consent_gate():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeOptionLocator:
        def __init__(self, options):
            self._options = list(options)

        def count(self):
            return len(self._options)

        def nth(self, index):
            return SimpleNamespace(inner_text=lambda: self._options[index])

    class FakeSelectLocator:
        def __init__(self):
            self.selected = None
            self.first = self

        def count(self):
            return 1

        def locator(self, selector):
            assert selector == "option"
            return FakeOptionLocator(
                [
                    "Select your location of residence and language",
                    "All Locations (English)",
                ]
            )

        def select_option(self, *, label):
            self.selected = label

    class FakeButtonLocator:
        def __init__(self):
            self.clicked = False
            self.first = self

        def count(self):
            return 1

        def click(self):
            self.clicked = True

    class FakeFileInputs:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class FakePage:
        def __init__(self):
            self.select = FakeSelectLocator()
            self.submit = FakeButtonLocator()
            self.waited_for_selector = None
            self.wait_calls = []

        def locator(self, selector):
            if selector == "input[type='file']":
                return FakeFileInputs(0)
            raise AssertionError(selector)

        def get_by_label(self, pattern):
            assert pattern.search("Location of Residence and Language")
            return self.select

        def get_by_role(self, role, name=None):
            assert role == "button"
            assert name.search("Submit")
            return self.submit

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    advanced = autofill._advance_jobvite_location_gate(page)

    assert advanced is True
    assert page.select.selected == "All Locations (English)"
    assert page.submit.clicked is True
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        15000,
    )


def test_jobvite_post_navigate_hook_skips_when_form_is_already_visible():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, labels):
            self._labels = list(labels)
            self.first = self

        def count(self):
            return len(self._labels)

        def click(self):
            raise AssertionError("collapsed-form expander should not be clicked when it is unavailable")

    class FakeFileInputs:
        def count(self):
            return 2

    class FakePage:
        def __init__(self):
            self.get_by_label_called = False
            self.role_queries = []

        def locator(self, selector):
            assert selector == "input[type='file']"
            return FakeFileInputs()

        def get_by_label(self, pattern):
            del pattern
            self.get_by_label_called = True
            raise AssertionError("location gate should not be queried once the form is visible")

        def get_by_role(self, role, name=None):
            assert role in {"button", "link"}
            self.role_queries.append((role, name.pattern if name is not None else None))
            return FakeButtonLocator([])

    page = FakePage()

    advanced = autofill._advance_jobvite_location_gate(page)

    assert advanced is False
    assert page.get_by_label_called is False
    assert page.role_queries == [
        ("button", "view full application form"),
        ("link", "view full application form"),
    ]


def test_jobvite_post_navigate_hook_expands_collapsed_form_before_skipping_file_input_gate():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, page, labels):
            self._page = page
            self._labels = list(labels)
            self.first = self

        def count(self):
            return len(self._labels)

        def click(self):
            assert self._labels == ["View Full Application Form"]
            self._page.expanded = True

    class FakeFileInputs:
        def count(self):
            return 1

    class FakePage:
        def __init__(self):
            self.expanded = False
            self.get_by_label_called = False
            self.waited_for_selector = None
            self.wait_calls = []

        def locator(self, selector):
            assert selector == "input[type='file']"
            return FakeFileInputs()

        def get_by_label(self, pattern):
            del pattern
            self.get_by_label_called = True
            raise AssertionError("location gate should not be queried when the collapsed full-form expander is present")

        def get_by_role(self, role, name=None):
            assert role in {"button", "link"}
            labels = ["View Full Application Form"] if (role == "button" and not self.expanded and name.search("View Full Application Form")) else []
            return FakeButtonLocator(self, labels)

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    advanced = autofill._advance_jobvite_location_gate(page)

    assert advanced is True
    assert page.expanded is True
    assert page.get_by_label_called is False
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        15000,
    )


def test_jobvite_post_navigate_hook_clicks_apply_link_when_landing_page_has_no_form_controls():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, page, labels):
            self._page = page
            self._labels = list(labels)
            self.first = self

        def count(self):
            return len(self._labels)

        def click(self):
            assert self._labels == ["Apply"]
            self._page.clicked = True
            self._page.form_visible = True

    class FakeFileInputs:
        def __init__(self, page):
            self._page = page

        def count(self):
            return 1 if self._page.form_visible else 0

    class FakeLocationSelect:
        first = None

        def count(self):
            return 0

    class FakePage:
        def __init__(self):
            self.clicked = False
            self.form_visible = False
            self.waited_for_selector = None

        def locator(self, selector):
            assert selector == "input[type='file']"
            return FakeFileInputs(self)

        def get_by_label(self, pattern):
            assert pattern.search("Location of Residence and Language")
            locator = FakeLocationSelect()
            locator.first = locator
            return locator

        def get_by_role(self, role, name=None):
            assert role in {"button", "link"}
            if name.search("View Full Application Form"):
                return FakeButtonLocator(self, [])
            if role == "link" and name.search("Apply"):
                return FakeButtonLocator(self, ["Apply"])
            return FakeButtonLocator(self, [])

        def wait_for_timeout(self, timeout_ms):
            del timeout_ms

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    advanced = autofill._advance_jobvite_location_gate(page)

    assert advanced is True
    assert page.clicked is True
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        15000,
    )


def test_jobvite_post_navigate_hook_clicks_apply_link_then_advances_location_gate():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeOptionLocator:
        def __init__(self, options):
            self._options = list(options)

        def count(self):
            return len(self._options)

        def nth(self, index):
            return SimpleNamespace(inner_text=lambda: self._options[index])

    class FakeSelectLocator:
        def __init__(self, page):
            self._page = page
            self.selected = None
            self.first = self

        def count(self):
            return 1 if self._page.stage == "location" else 0

        def locator(self, selector):
            assert selector == "option"
            return FakeOptionLocator(
                [
                    "Select your location of residence and language",
                    "US / UK / India",
                    "Poland",
                ]
            )

        def select_option(self, *, label):
            self.selected = label
            self._page.stage = "form"

    class FakeButtonLocator:
        def __init__(self, page, labels, on_click=None):
            self._page = page
            self._labels = list(labels)
            self._on_click = on_click
            self.first = self

        def count(self):
            return len(self._labels)

        def click(self):
            if self._on_click is not None:
                self._on_click()
            self._page.clicked_labels.extend(self._labels)

    class FakeFileInputs:
        def __init__(self, page):
            self._page = page

        def count(self):
            return 1 if self._page.stage == "form" else 0

    class FakePage:
        def __init__(self):
            self.stage = "landing"
            self.clicked_labels = []
            self.waited_for_selector = None
            self.wait_calls = []
            self.select = FakeSelectLocator(self)

        def locator(self, selector):
            assert selector == "input[type='file']"
            return FakeFileInputs(self)

        def get_by_label(self, pattern):
            assert pattern.search("Location of Residence and Language")
            return self.select

        def get_by_role(self, role, name=None):
            assert role in {"button", "link"}
            if name.search("View Full Application Form"):
                return FakeButtonLocator(self, [])
            if role == "link" and name.search("Apply") and self.stage == "landing":
                return FakeButtonLocator(self, ["Apply"], on_click=lambda: setattr(self, "stage", "location"))
            return FakeButtonLocator(self, [])

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    advanced = autofill._advance_jobvite_location_gate(page)

    assert advanced is True
    assert page.clicked_labels == ["Apply"]
    assert page.select.selected == "US / UK / India"
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        15000,
    )


def test_jobvite_expand_full_application_form_when_collapsed():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, page):
            self._page = page
            self.first = self

        def count(self):
            return 0 if self._page.expanded else 1

        def click(self):
            self._page.expanded = True

    class FakeFileInputs:
        def __init__(self, page):
            self._page = page

        def count(self):
            return 2 if self._page.expanded else 0

    class FakePage:
        def __init__(self):
            self.expanded = False
            self.waited_for_selector = None
            self.wait_calls = []

        def locator(self, selector):
            assert selector == "input[type='file']"
            return FakeFileInputs(self)

        def get_by_role(self, role, name=None):
            assert role == "button"
            assert name.search("View Full Application Form")
            return FakeButtonLocator(self)

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    expanded = autofill._expand_jobvite_full_application_form(page)

    assert expanded is True
    assert page.expanded is True
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        15000,
    )


def test_jobvite_post_fill_hook_advances_to_final_review_boundary():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, page, labels):
            self._page = page
            self._labels = list(labels)
            self.first = self

        def count(self):
            return len(self._labels)

        def is_visible(self):
            return bool(self._labels)

        def is_enabled(self):
            return bool(self._labels)

        def click(self):
            assert self._labels == ["Next"]
            self._page.phase += 1

    class FakePage:
        def __init__(self):
            self.phase = 0
            self.wait_calls = []
            self.waited_for_selector = None

        def get_by_role(self, role, name=None):
            assert role == "button"
            labels = ["Next"] if self.phase == 0 else ["Submit Application"]
            matched = [label for label in labels if name.search(label)]
            return FakeButtonLocator(self, matched)

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

        def evaluate(self, script):
            del script
            return {
                "url": "https://jobs.jobvite.com/company/job/abc",
                "controls": [f"phase-{self.phase}"],
                "buttons": ["Next"] if self.phase == 0 else ["Submit Application"],
            }

    page = FakePage()

    advanced = autofill._advance_jobvite_to_final_review_boundary(page)

    assert advanced is True
    assert page.phase == 1
    assert page.waited_for_selector == (
        "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')",
        5000,
    )


def test_jobvite_post_fill_hook_skips_boundary_advance_when_required_fields_remain_visible():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")
    page = object()

    with mock.patch.object(
        autofill,
        "detect_live_required_unfilled_fields",
        return_value=[{"field_name": "input-ymIyZfwl", "label": "Work Status*"}],
    ), mock.patch.object(autofill, "_advance_jobvite_page") as advance_page:
        advanced = autofill._advance_jobvite_to_final_review_boundary(page)

    assert advanced is False
    advance_page.assert_not_called()


def test_jobvite_advance_page_requires_visible_page_change():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    class FakeButtonLocator:
        def __init__(self, labels):
            self._labels = list(labels)
            self.first = self

        def count(self):
            return len(self._labels)

        def is_visible(self):
            return bool(self._labels)

        def is_enabled(self):
            return bool(self._labels)

        def click(self):
            return None

    class FakePage:
        def __init__(self):
            self.wait_calls = []
            self.waited_for_selector = None

        def get_by_role(self, role, name=None):
            assert role == "button"
            labels = ["Next"] if name.search("Next") else []
            matched = [label for label in labels if name.search(label)]
            return FakeButtonLocator(matched)

        def wait_for_timeout(self, timeout_ms):
            self.wait_calls.append(timeout_ms)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    with mock.patch.object(autofill, "_jobvite_page_signature", side_effect=lambda _: "page-1"):
        advanced = autofill._advance_jobvite_page(page)

    assert advanced is False


def test_run_browser_registers_post_navigate_hook():
    autofill = load_module("autofill_jobvite", "scripts/autofill_jobvite.py")

    with mock.patch("autofill_pipeline.run_browser_pipeline", return_value=0) as run_browser_pipeline:
        rc = autofill._run_browser(Path("/tmp/payload.json"), headless=True, submit=False)

    assert rc == 0
    kwargs = run_browser_pipeline.call_args.kwargs
    assert callable(kwargs["post_navigate_hook"])
