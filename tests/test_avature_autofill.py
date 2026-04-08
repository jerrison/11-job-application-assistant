import importlib.util
import os
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


class _EmptyLocator:
    def count(self) -> int:
        return 0

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return False


class _AvaturePage:
    def __init__(self, text: str, *, url: str = "https://careers.jacobs.com/en_US/careers/Error"):
        self.text = text
        self.url = url

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.text

    def locator(self, selector: str):
        del selector
        return _EmptyLocator()

    def get_by_role(self, role: str, name: str):
        del role, name
        return _EmptyLocator()


class _PasswordLocator:
    def count(self) -> int:
        return 1


class _ButtonControl:
    def __init__(self, *, visible: bool = True, click_exc: Exception | None = None, dispatch_exc: Exception | None = None):
        self.visible = visible
        self.click_exc = click_exc
        self.dispatch_exc = dispatch_exc
        self.click_calls = 0
        self.dispatch_calls = 0
        self.scroll_calls = 0

    def count(self) -> int:
        return 1 if self.visible else 0

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return self.visible

    def scroll_into_view_if_needed(self):
        self.scroll_calls += 1

    def click(self, timeout=None, no_wait_after=None):
        del timeout, no_wait_after
        self.click_calls += 1
        if self.click_exc is not None:
            raise self.click_exc

    def dispatch_event(self, event: str):
        self.dispatch_calls += 1
        if self.dispatch_exc is not None:
            raise self.dispatch_exc
        assert event == "click"


class _ButtonPage:
    def __init__(self, control: _ButtonControl, *, accepted_names: set[str] | None = None):
        self.control = control
        self.accepted_names = accepted_names
        self.wait_calls: list[int] = []

    def evaluate(self, script: str):
        assert script == "window.scrollTo(0, document.body.scrollHeight)"

    def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    def get_by_role(self, role: str, name: str):
        del role
        if self.accepted_names is not None and name not in self.accepted_names:
            return _EmptyLocator()
        return self.control

    def locator(self, selector: str):
        del selector
        return _EmptyLocator()


class _FieldsetLegendLocator:
    def __init__(self, text: str | None):
        self.text = text
        self.text_calls = 0

    def count(self) -> int:
        return 0 if self.text is None else 1

    @property
    def first(self):
        return self

    def text_content(self) -> str:
        self.text_calls += 1
        if self.text is None:
            raise AssertionError("text_content() should not be called when legend is missing")
        return self.text


class _RadioLocator:
    def __init__(self, count: int = 0):
        self._count = count

    def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self


class _Fieldset:
    def __init__(self, legend: str | None, *, radio_count: int = 0):
        self.legend_locator = _FieldsetLegendLocator(legend)
        self.radio_locator = _RadioLocator(radio_count)

    def locator(self, selector: str):
        if selector == "legend":
            return self.legend_locator
        if selector == "input[type='radio']":
            return self.radio_locator
        raise AssertionError(f"unexpected selector: {selector}")


class _FieldsetCollection:
    def __init__(self, fieldsets):
        self._fieldsets = fieldsets

    def all(self):
        return self._fieldsets


class _FieldsetPage:
    def __init__(self, fieldsets):
        self._fieldsets = fieldsets

    def locator(self, selector: str):
        if selector == "fieldset":
            return _FieldsetCollection(self._fieldsets)
        raise AssertionError(f"unexpected selector: {selector}")


class _PrefilledTextInput:
    def __init__(self, value: str):
        self.value = value

    def input_value(self) -> str:
        return self.value

    def is_checked(self) -> bool:
        return False


class AvatureUrlTests(unittest.TestCase):
    def test_looks_like_avature_url_detects_direct_avature_host(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        self.assertTrue(
            job_board_urls.looks_like_avature_url(
                "https://intuit.avature.net/externalCareers/JobApplication?pipelineId=19076"
            )
        )

    def test_looks_like_avature_url_detects_branded_job_detail_path(self):
        job_board_urls = load_module("job_board_urls", "scripts/job_board_urls.py")

        self.assertTrue(
            job_board_urls.looks_like_avature_url(
                "https://careers.jacobs.com/en_US/careers/JobDetail/Principal-Product-Manager/35978?Src=JB-10147"
            )
        )

    def test_canonical_avature_application_url_converts_job_detail_to_application_methods(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        self.assertEqual(
            avature._canonical_avature_application_url(
                "https://careers.jacobs.com/en_US/careers/JobDetail/Principal-Product-Manager/35978?Src=JB-10147"
            ),
            "https://careers.jacobs.com/en_US/careers/ApplicationMethods?jobId=35978",
        )

    def test_canonical_avature_application_url_preserves_direct_job_application_url(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")
        url = "https://intuit.avature.net/externalCareers/JobApplication?pipelineId=19076&cid=directBookmarked"

        self.assertEqual(avature._canonical_avature_application_url(url), url)

    def test_avature_credentials_fall_back_to_workday_env(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        fake_profile = mock.Mock(verification_code_email="profile@example.com")
        with (
            mock.patch.dict(
                os.environ,
                {
                    "WORKDAY_EMAIL": "workday@example.com",
                    "WORKDAY_PASSWORD": "TestPass1!",
                },
                clear=False,
            ),
            mock.patch.object(avature, "parse_application_profile", return_value=fake_profile),
        ):
            email, password = avature._avature_credentials()

        self.assertEqual(email, "workday@example.com")
        self.assertEqual(password, "TestPass1!")

    def test_answer_from_classifier_prefers_careers_site_for_corporate_website_source(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        application_profile = mock.Mock(how_did_you_hear="Corporate website", age_range=None)
        options = [
            "Career Fair",
            "Careers Site",
            "Job Board",
            "LinkedIn",
        ]

        self.assertEqual(
            avature._answer_from_classifier(
                "Please indicate how you heard about this job at Jacobs",
                application_profile,
                options,
            ),
            ("Careers Site", "application_profile.md"),
        )

    def test_parse_candidate_contact_details_extracts_home_address(self):
        application_models = load_module("application_models", "scripts/application_models.py")

        details = application_models.parse_candidate_contact_details(
            "Logistical Information\n* Lives in 720 Gough St APT 27, San Francisco CA, 94102\n"
        )

        self.assertEqual(details.street_address, "720 Gough St APT 27")
        self.assertEqual(details.city, "San Francisco")
        self.assertEqual(details.state, "CA")
        self.assertEqual(details.zip_code, "94102")

    def test_resolved_job_title_prefers_jobdetail_slug_when_saved_title_is_generic(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        self.assertEqual(
            avature._resolved_job_title(
                {
                    "jd_title": "Jacobs",
                    "company_proper": "StreetLight",
                },
                "https://careers.jacobs.com/en_US/careers/JobDetail/Principal-Product-Manager/35978?Src=JB-10147",
            ),
            "Principal Product Manager",
        )

    def test_answer_from_classifier_uses_truthful_no_for_sponsorship_questions(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        application_profile = mock.Mock(
            how_did_you_hear="Corporate website",
            age_range=None,
            authorized_to_work_unconditionally=True,
            require_sponsorship_now=False,
            require_sponsorship_future=False,
            sponsorship_answer="No",
            work_authorization_statement="I am authorized to work in the United States.",
            comfortable_with_posted_salary=True,
        )

        self.assertEqual(
            avature._answer_from_classifier(
                "Will you now or in the future require sponsorship for employment visa status to work for Jacobs in the U.S.?",
                application_profile,
                ["Yes", "No"],
            ),
            ("No", "application_profile.md"),
        )

    def test_binary_answer_defaults_conflict_questions_to_no(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        application_profile = mock.Mock(
            how_did_you_hear="Corporate website",
            age_range=None,
            authorized_to_work_unconditionally=True,
            require_sponsorship_now=False,
            require_sponsorship_future=False,
            sponsorship_answer="No",
            work_authorization_statement="I am authorized to work in the United States.",
            comfortable_with_posted_salary=True,
            text_message_consent=False,
        )

        self.assertEqual(
            avature._binary_answer_for_label(
                "Do you have a relative(s) working at Jacobs?",
                ["Yes", "No"],
                application_profile,
                {"company": "StreetLight"},
            ),
            ("No", "deterministic"),
        )
        self.assertEqual(
            avature._binary_answer_for_label(
                "Have you previously worked at Jacobs?",
                ["Yes", "No"],
                application_profile,
                {"company": "StreetLight"},
            ),
            ("No", "master_resume.md"),
        )

    def test_binary_answer_defaults_talent_community_opt_in_to_no(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        application_profile = mock.Mock(
            how_did_you_hear="Corporate website",
            age_range=None,
            authorized_to_work_unconditionally=True,
            require_sponsorship_now=False,
            require_sponsorship_future=False,
            sponsorship_answer="No",
            work_authorization_statement="I am authorized to work in the United States.",
            comfortable_with_posted_salary=True,
            text_message_consent=False,
        )

        self.assertEqual(
            avature._binary_answer_for_label(
                "Join Intuit's Talent Community",
                ["Yes", "No"],
                application_profile,
                {"company": "Intuit"},
            ),
            ("No", "deterministic"),
        )

    def test_detect_current_page_treats_existing_account_error_as_entry_gate(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        page = _AvaturePage(
            "An error has occurred Can't create user: There is an existing account with the email address you entered."
        )

        self.assertEqual(avature._detect_current_page(page), avature.PAGE_ENTRY)

    def test_extract_avature_entry_error_message_returns_existing_account_copy(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        page = _AvaturePage(
            "An error has occurred Can't create user: There is an existing account with the email address you entered."
        )

        self.assertEqual(
            avature._entry_gate_error_message(page),
            "Can't create user: There is an existing account with the email address you entered.",
        )

    def test_extract_avature_entry_error_message_returns_login_failure_copy(self):
        avature = load_module("autofill_avature_login_error", "scripts/autofill_avature.py")

        page = _AvaturePage("Login Already registered? The username or password may be incorrect, or access might be restricted.")

        self.assertEqual(
            avature._entry_gate_error_message(page),
            "The username or password may be incorrect, or access might be restricted.",
        )

    def test_handle_entry_gate_prefers_sign_in_before_resume_entry_on_mixed_gate(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        page = mock.Mock(url="https://careers.jacobs.com/en_US/careers/Registering")
        page.locator.side_effect = lambda selector: _PasswordLocator() if selector == "input[type='password']" else _EmptyLocator()

        calls: list[str] = []

        with (
            mock.patch.object(avature, "_page_text", return_value="Upload Resume Login"),
            mock.patch.object(
                avature,
                "_try_sign_in",
                side_effect=lambda *_args, **_kwargs: calls.append("sign_in") or True,
            ),
            mock.patch.object(
                avature,
                "_try_resume_entry",
                side_effect=lambda *_args, **_kwargs: calls.append("resume") or True,
            ),
        ):
            advanced = avature._handle_entry_gate(page, {}, "candidate@example.com", "SecretPass1!")

        self.assertTrue(advanced)
        self.assertEqual(calls, ["sign_in"])

    def test_handle_entry_gate_existing_account_error_clicks_login_then_signs_in(self):
        avature = load_module("autofill_avature", "scripts/autofill_avature.py")

        page = mock.Mock(url="https://intuit.avature.net/en_US/externalCareers/Error#main")
        page.locator.side_effect = lambda selector: _EmptyLocator()

        calls: list[str] = []

        with (
            mock.patch.object(
                avature,
                "_page_text",
                return_value="An error has occurred. Can't create user: There is an existing account with the email address you entered. Login",
            ),
            mock.patch.object(
                avature,
                "_click_first_visible",
                side_effect=lambda *_args, **_kwargs: calls.append("login_link") or True,
            ),
            mock.patch.object(
                avature,
                "_try_sign_in",
                side_effect=lambda *_args, **_kwargs: calls.append("sign_in") or True,
            ),
            mock.patch.object(avature, "_discover_avature_application_url", return_value=None),
        ):
            advanced = avature._handle_entry_gate(page, {}, "candidate@example.com", "SecretPass1!")

        self.assertTrue(advanced)
        self.assertEqual(calls, ["login_link", "sign_in"])

    def test_handle_entry_gate_existing_account_error_without_login_path_returns_false(self):
        avature = load_module("autofill_avature_existing_account_stop", "scripts/autofill_avature.py")

        page = mock.Mock(url="https://intuit.avature.net/en_US/externalCareers/Error#main")
        page.locator.side_effect = lambda selector: _EmptyLocator()

        with (
            mock.patch.object(
                avature,
                "_page_text",
                return_value="An error has occurred. Can't create user: There is an existing account with the email address you entered.",
            ),
            mock.patch.object(avature, "_click_first_visible", return_value=False),
            mock.patch.object(avature, "_discover_avature_application_url", return_value="https://intuit.avature.net/en_US/externalCareers/JobApplication?pipelineId=19349"),
        ):
            advanced = avature._handle_entry_gate(page, {}, "candidate@example.com", "SecretPass1!")

        self.assertFalse(advanced)

    def test_click_action_button_with_fallback_dispatches_click_after_timeout(self):
        avature = load_module("autofill_avature_click_fallback", "scripts/autofill_avature.py")
        control = _ButtonControl(click_exc=RuntimeError("click timed out"))

        avature._click_action_button_with_fallback(control, label="Continue")

        self.assertEqual(control.click_calls, 1)
        self.assertEqual(control.dispatch_calls, 1)

    def test_click_action_button_with_fallback_raises_original_error_when_dispatch_fails(self):
        avature = load_module("autofill_avature_click_fallback_error", "scripts/autofill_avature.py")
        click_error = RuntimeError("click timed out")
        control = _ButtonControl(click_exc=click_error, dispatch_exc=RuntimeError("dispatch failed"))

        with self.assertRaises(RuntimeError) as excinfo:
            avature._click_action_button_with_fallback(control, label="Continue")

        self.assertIs(excinfo.exception, click_error)
        self.assertEqual(control.click_calls, 1)
        self.assertEqual(control.dispatch_calls, 1)

    def test_click_next_button_uses_fallback_when_primary_click_times_out(self):
        avature = load_module("autofill_avature_next_click", "scripts/autofill_avature.py")
        control = _ButtonControl(click_exc=RuntimeError("click timed out"))
        page = _ButtonPage(control)

        result = avature._click_next_button(page)

        self.assertTrue(result)
        self.assertEqual(control.click_calls, 1)
        self.assertEqual(control.dispatch_calls, 1)
        self.assertEqual(page.wait_calls, [500, 2500])

    def test_click_next_button_accepts_save_label(self):
        avature = load_module("autofill_avature_next_save", "scripts/autofill_avature.py")
        control = _ButtonControl()
        page = _ButtonPage(control, accepted_names={"Save"})

        result = avature._click_next_button(page)

        self.assertTrue(result)
        self.assertEqual(control.click_calls, 1)
        self.assertEqual(control.dispatch_calls, 0)
        self.assertEqual(page.wait_calls, [500, 2500])

    def test_collect_radio_groups_skips_fieldsets_without_legends(self):
        avature = load_module("autofill_avature_radio_groups", "scripts/autofill_avature.py")
        missing_legend = _Fieldset(None, radio_count=2)
        titled_fieldset = _Fieldset("Personal information", radio_count=0)
        page = _FieldsetPage([missing_legend, titled_fieldset])

        question_specs, field_elements, handled_names = avature._collect_radio_groups(page)

        self.assertEqual(question_specs, [])
        self.assertEqual(field_elements, [])
        self.assertEqual(handled_names, set())
        self.assertEqual(missing_legend.legend_locator.text_calls, 0)
        self.assertEqual(titled_fieldset.legend_locator.text_calls, 1)

    def test_fill_application_questions_skips_prefilled_profile_like_fields_before_llm_generation(self):
        avature = load_module("autofill_avature_prefilled_profile_skip", "scripts/autofill_avature.py")
        password_input = _PrefilledTextInput("JerrisonLi94102!")
        password_confirmation_input = _PrefilledTextInput("JerrisonLi94102!")
        question_specs = [
            {
                "field_name": "password",
                "label": "Password",
                "kind": "text",
                "required": True,
                "options": [],
            },
            {
                "field_name": "password_confirmation",
                "label": "Password confirmation",
                "kind": "text",
                "required": True,
                "options": [],
            },
        ]
        field_elements = [
            ("Password", password_input, "text", []),
            ("Password confirmation", password_confirmation_input, "text", []),
        ]

        with (
            mock.patch.object(avature, "_collect_question_fields", return_value=(question_specs, field_elements)),
            mock.patch.object(
                avature,
                "generate_application_answers",
                side_effect=AssertionError("LLM generation should not run for prefilled profile-like fields"),
            ),
        ):
            filled = avature._fill_application_questions(
                page=mock.Mock(),
                out_dir=PROJECT_ROOT,
                meta={},
                provider="openai",
                application_profile=mock.Mock(),
                payload={},
            )

        self.assertEqual(filled, [])

    def test_profile_question_matcher_does_not_treat_statement_as_state(self):
        avature = load_module("autofill_avature_profile_matcher", "scripts/autofill_avature.py")

        self.assertFalse(
            avature._looks_like_profile_question(
                "By submitting this form, you acknowledge that Intuit may use your personal information as described in Intuit’s Global Applicant and Candidate Privacy Statement."
            )
        )
        self.assertTrue(avature._looks_like_profile_question("Home state/province"))
        self.assertTrue(avature._looks_like_profile_question("Professional site URL (i.e. LinkedIn)"))

    def test_fill_application_questions_skips_acknowledgement_like_text_fields_before_llm_generation(self):
        avature = load_module("autofill_avature_ack_text_skip", "scripts/autofill_avature.py")
        acknowledgement_input = _PrefilledTextInput("")
        question_specs = [
            {
                "field_name": "privacy_statement",
                "label": "By submitting this form, you acknowledge that Intuit may use your personal information as described in Intuit’s Global Applicant and Candidate Privacy Statement.",
                "kind": "text",
                "required": True,
                "options": [],
            }
        ]
        field_elements = [
            (
                "By submitting this form, you acknowledge that Intuit may use your personal information as described in Intuit’s Global Applicant and Candidate Privacy Statement.",
                acknowledgement_input,
                "text",
                [],
            )
        ]

        with (
            mock.patch.object(avature, "_collect_question_fields", return_value=(question_specs, field_elements)),
            mock.patch.object(
                avature,
                "generate_application_answers",
                side_effect=AssertionError("LLM generation should not run for acknowledgement-like text fields"),
            ),
        ):
            filled = avature._fill_application_questions(
                page=mock.Mock(),
                out_dir=PROJECT_ROOT,
                meta={},
                provider="openai",
                application_profile=mock.Mock(),
                payload={},
            )

        self.assertEqual(filled, [])
