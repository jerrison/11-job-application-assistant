import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_board_urls import (
    canonical_eightfold_job_url,
    looks_like_eightfold_url,
)


def test_eightfold_direct_host():
    assert looks_like_eightfold_url("https://paypal.eightfold.ai/careers?pid=123")


def test_eightfold_subdomain():
    assert looks_like_eightfold_url("https://netflix.eightfold.ai/careers/apply?pid=456&domain=netflix.com")


def test_not_eightfold():
    assert not looks_like_eightfold_url("https://boards.greenhouse.io/company/jobs/123")
    assert not looks_like_eightfold_url("https://jobs.lever.co/company/abc")


def test_canonical_strips_tracking_params():
    url = (
        "https://paypal.eightfold.ai/careers"
        "?domain=paypal.com&Codes=W-LINKEDIN&query=R0132250"
        "&start=0&location=San+Francisco&pid=274916506310"
        "&sort_by=relevance&filter_distance=80&filter_include_remote=1"
    )
    canon = canonical_eightfold_job_url(url)
    assert "Codes=" not in canon
    assert "sort_by=" not in canon
    assert "filter_distance=" not in canon
    assert "filter_include_remote=" not in canon
    assert "start=" not in canon
    assert "location=" not in canon
    assert "pid=274916506310" in canon
    assert "domain=paypal.com" in canon
    assert "query=R0132250" in canon


def test_canonical_preserves_apply_url():
    url = "https://paypal.eightfold.ai/careers/apply?pid=274916506310&domain=paypal.com"
    canon = canonical_eightfold_job_url(url)
    assert "pid=274916506310" in canon
    assert "domain=paypal.com" in canon


def test_canonical_noop_for_non_eightfold():
    url = "https://boards.greenhouse.io/company/jobs/123"
    assert canonical_eightfold_job_url(url) == url


# --- Board routing tests (Task 3) ---

from submit_application import _board_for_url


def test_board_routing_eightfold():
    assert _board_for_url("https://paypal.eightfold.ai/careers/apply?pid=123") == "eightfold"


# --- Deterministic override tests (Task 5) ---


def test_eightfold_deterministic_previous_employee():
    """Previous employee questions should always answer No."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic("Are you a previous Employee of PayPal or any of its subsidiaries?", [])
    assert answer is not None
    assert answer.casefold() == "no"


def test_eightfold_deterministic_pep():
    """PEP (Politically Exposed Person) questions should always answer No."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "I am related to or associated with a Politically Exposed Person (PEP).",
        ["Yes", "No"],
    )
    assert answer is not None
    assert answer.casefold() == "no"


def test_eightfold_deterministic_nda():
    """NDA acknowledgment should always be checked."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Yes, I acknowledge and agree to abide by the terms of this Nondisclosure Agreement.",
        [],
    )
    assert answer is not None


def test_eightfold_deterministic_privacy():
    """Privacy consent should always be checked."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Yes, I have read and consent to this Privacy Statement.",
        [],
    )
    assert answer is not None


def test_eightfold_deterministic_hybrid_setting():
    """Hybrid-setting prompts should default to Yes under the shared positive-fit policy."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Are you comfortable working in a hybrid setting?",
        ["Yes", "No"],
    )
    assert answer is not None
    assert answer.casefold() == "yes"


def test_eightfold_deterministic_culture_careers_optin_defaults_to_no():
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Would you like to stay up to date with Microsoft Culture and Careers content?",
        ["Yes", "No"],
    )
    assert answer is not None
    assert answer.casefold() == "no"


class _EmptyLocator:
    def count(self) -> int:
        return 0

    @property
    def first(self):
        return self

    def locator(self, selector: str):
        del selector
        return self


class _FilledField:
    def __init__(self):
        self.value = ""
        self.scroll_calls = 0

    def count(self) -> int:
        return 1

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self):
        self.scroll_calls += 1

    def fill(self, value: str):
        self.value = value

    def locator(self, selector: str):
        del selector
        return _EmptyLocator()

    def click(self):
        pass


class _KeyboardOpenCombobox(_FilledField):
    def __init__(self):
        super().__init__()
        self.click_calls = 0
        self.focus_calls = 0
        self.pressed: list[str] = []
        self.opened = False

    def click(self):
        self.click_calls += 1

    def focus(self):
        self.focus_calls += 1

    def press(self, key: str):
        self.pressed.append(key)
        if key == "ArrowDown":
            self.opened = True

    def get_attribute(self, name: str):
        if name == "aria-expanded":
            return "true" if self.opened else "false"
        return None


class _ReadonlyDateCombobox(_FilledField):
    def __init__(self):
        super().__init__()
        self.evaluate_calls: list[str] = []
        self.filled_values: list[str] = []

    def get_attribute(self, name: str):
        if name == "readonly":
            return ""
        return None

    def fill(self, value: str):
        self.filled_values.append(value)
        raise RuntimeError("readonly input")

    def evaluate(self, expression: str, value: str):
        self.evaluate_calls.append(value)
        self.value = value


class _ValueField(_FilledField):
    def __init__(self, value: str):
        super().__init__()
        self.value = value

    def input_value(self) -> str:
        return self.value

    def get_attribute(self, name: str):
        if name == "value":
            return self.value
        return None


class _ButtonLocator:
    def __init__(self, *, visible: bool = True, attributes: dict[str, str] | None = None):
        self.visible = visible
        self.click_calls = 0
        self.attributes = attributes or {}

    def count(self) -> int:
        return 1 if self.visible else 0

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return self.visible

    def scroll_into_view_if_needed(self):
        pass

    def click(self):
        self.click_calls += 1

    def get_attribute(self, name: str):
        return self.attributes.get(name)


class _Option:
    def __init__(self, text: str):
        self.text = text
        self.click_calls = 0

    def inner_text(self) -> str:
        return self.text

    def click(self):
        self.click_calls += 1


class _JsClickableOption(_Option):
    def __init__(self, text: str):
        super().__init__(text)
        self.evaluate_calls = 0

    def evaluate(self, expression: str):
        assert expression == "(el) => el.click()"
        self.evaluate_calls += 1


class _OptionLocator:
    def __init__(self, options: list[_Option]):
        self.options = options

    def count(self) -> int:
        return len(self.options)

    def nth(self, index: int) -> _Option:
        return self.options[index]

    @property
    def first(self):
        return self.nth(0)


class _LiveOptionLocator:
    def __init__(self, get_options):
        self.get_options = get_options

    def count(self) -> int:
        return len(self.get_options())

    def nth(self, index: int) -> _Option:
        return self.get_options()[index]

    @property
    def first(self):
        return self.nth(0)


def test_fill_step_leaves_visible_self_id_unconfirmed_when_confirmation_fails():
    from autofill_eightfold import _fill_step

    step = {
        "field_name": "gender",
        "label": "Gender",
        "kind": "combobox",
        "value": "Male",
        "source": "application_profile.md",
        "blocks_draft_completion": True,
        "blocker_kind": "visible_self_id",
    }

    with mock.patch("autofill_eightfold._fill_eightfold_combobox", return_value=True):
        with mock.patch("autofill_eightfold._confirm_eightfold_combobox", return_value=False):
            _fill_step(page=mock.Mock(), step=step)

    assert not step.get("filled", False)
    assert step["status"] == "planned"
    assert "could not confirm" in step["note"].lower()


def test_fill_step_text_falls_back_to_get_by_label_when_textbox_role_missing():
    from autofill_eightfold import _fill_step

    field = _FilledField()
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "textbox":
            return _EmptyLocator()
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = field

    step = {
        "field_name": "first_name",
        "label": "First Name",
        "kind": "text",
        "value": "Jerrison",
        "source": "master_resume.md",
    }

    _fill_step(page=page, step=step)

    assert step.get("filled") is True
    assert field.value == "Jerrison"


def test_fill_step_dismisses_transient_dialogs_before_interacting():
    from autofill_eightfold import _fill_step

    field = _FilledField()
    page = mock.Mock()
    page.get_by_role.return_value = _EmptyLocator()
    page.get_by_label.return_value = field

    step = {
        "field_name": "first_name",
        "label": "First Name",
        "kind": "text",
        "value": "Jerrison",
        "source": "master_resume.md",
    }

    with mock.patch("autofill_eightfold._dismiss_cookie_banner") as dismiss_cookie, mock.patch(
        "autofill_eightfold._dismiss_privacy_dialog"
    ) as dismiss_privacy:
        _fill_step(page=page, step=step)

    dismiss_cookie.assert_called_once_with(page)
    dismiss_privacy.assert_called_once_with(page)
    assert field.value == "Jerrison"


def test_fill_step_date_sets_readonly_datepicker_value_via_dom_events():
    from autofill_eightfold import _fill_step

    field = _ReadonlyDateCombobox()
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return field
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = field

    step = {
        "field_name": "acknowledgement",
        "label": "acknowledgement",
        "kind": "date",
        "value": "2026-04-05",
        "source": "deterministic_override",
    }

    _fill_step(page=page, step=step)

    assert step.get("filled") is True
    assert field.evaluate_calls == ["2026-04-05"]
    assert field.value == "2026-04-05"


def test_fill_step_date_marks_missing_field_as_skipped_not_found():
    from autofill_eightfold import _fill_step

    page = mock.Mock()
    page.get_by_role.return_value = _EmptyLocator()
    page.get_by_label.return_value = _EmptyLocator()
    page.locator.return_value = _EmptyLocator()

    step = {
        "field_name": "acknowledgment_date",
        "label": "acknowledgement",
        "kind": "date",
        "value": "2026-04-05",
        "source": "deterministic_override",
    }

    _fill_step(page=page, step=step)

    assert step.get("filled") is not True
    assert step.get("status") == "skipped_not_found"


def test_fill_eightfold_combobox_falls_back_to_get_by_label_when_role_missing():
    from autofill_eightfold import _fill_eightfold_combobox

    combobox = _FilledField()
    expand = _ButtonLocator()
    combobox.locator = lambda selector: expand if selector == "xpath=following-sibling::button | ../button" else _EmptyLocator()
    options = [_Option("United States of America"), _Option("Canada")]
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return _EmptyLocator()
        if role == "option":
            return _OptionLocator(options)
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = combobox

    assert _fill_eightfold_combobox(page, "Country", "United States of America") is True
    assert expand.click_calls == 1
    assert options[0].click_calls == 1


def test_fill_eightfold_combobox_matches_company_website_aliases_for_referral_source():
    from autofill_eightfold import _fill_eightfold_combobox

    combobox = _FilledField()
    expand = _ButtonLocator()
    combobox.locator = lambda selector: expand if selector == "xpath=following-sibling::button | ../button" else _EmptyLocator()
    options = [_Option("Company website"), _Option("LinkedIn")]
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return _EmptyLocator()
        if role == "option":
            return _OptionLocator(options)
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = combobox

    assert _fill_eightfold_combobox(page, "How did you hear about us?", "Corporate website") is True
    assert expand.click_calls == 1
    assert options[0].click_calls == 1


def test_fill_eightfold_combobox_matches_gender_identity_alias_against_man_option():
    from autofill_eightfold import _fill_eightfold_combobox

    combobox = _FilledField()
    expand = _ButtonLocator()
    combobox.locator = lambda selector: expand if selector == "xpath=following-sibling::button | ../button" else _EmptyLocator()
    options = [_Option("Man"), _Option("Woman")]
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return _EmptyLocator()
        if role == "option":
            return _OptionLocator(options)
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = combobox

    assert _fill_eightfold_combobox(page, "Gender", "Male", profile_field="gender_identity") is True
    assert expand.click_calls == 1
    assert options[0].click_calls == 1


def test_fill_eightfold_combobox_skips_presentational_wrapper_button_and_uses_keyboard_fallback():
    from autofill_eightfold import _fill_eightfold_combobox

    combobox = _KeyboardOpenCombobox()
    presentational_button = _ButtonLocator(attributes={"aria-hidden": "true", "role": "presentation"})
    combobox.locator = (
        lambda selector: presentational_button if selector == "xpath=../div//button[1]" else _EmptyLocator()
    )
    options = [_Option("Direct Source Candidates"), _Option("LinkedIn")]
    live_options = _LiveOptionLocator(lambda: options if combobox.opened else [])
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return _EmptyLocator()
        if role == "option":
            return live_options
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = combobox

    assert _fill_eightfold_combobox(page, "How did you hear about us?", "Corporate website") is True
    assert presentational_button.click_calls == 0
    assert combobox.click_calls == 0
    assert combobox.focus_calls >= 1
    assert "ArrowDown" in combobox.pressed
    assert options[0].click_calls == 1


def test_fill_eightfold_combobox_prefers_dom_click_for_option_selection():
    from autofill_eightfold import _fill_eightfold_combobox

    combobox = _KeyboardOpenCombobox()
    options = [_JsClickableOption("Direct Source Candidates"), _Option("LinkedIn")]
    live_options = _LiveOptionLocator(lambda: options if combobox.opened else [])
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        del name
        if role == "combobox":
            return _EmptyLocator()
        if role == "option":
            return live_options
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role
    page.get_by_label.return_value = combobox

    assert _fill_eightfold_combobox(page, "How did you hear about us?", "Corporate website") is True
    assert options[0].evaluate_calls == 1
    assert options[0].click_calls == 0


def test_confirm_eightfold_combobox_accepts_veteran_status_alias_visible_in_input_value():
    from autofill_eightfold import _confirm_eightfold_combobox

    field = _ValueField("I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN")
    page = mock.Mock()
    page.get_by_role.return_value = field

    assert _confirm_eightfold_combobox(page, "Veteran Status", "I am not a protected veteran") is True


def test_dismiss_privacy_dialog_accepts_i_agree_button_variant():
    from autofill_eightfold import _dismiss_privacy_dialog

    agree = _ButtonLocator()
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        if role != "button":
            return _EmptyLocator()
        if name == "I Agree":
            return agree
        if hasattr(name, "search") and name.search("I Agree"):
            return agree
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role

    _dismiss_privacy_dialog(page)

    assert agree.click_calls == 1
    page.wait_for_timeout.assert_called_once_with(1000)


def test_dismiss_privacy_dialog_accepts_i_understand_button_variant():
    from autofill_eightfold import _dismiss_privacy_dialog

    understand = _ButtonLocator()
    page = mock.Mock()

    def get_by_role(role: str, name=None):
        if role != "button":
            return _EmptyLocator()
        if name == "I understand":
            return understand
        if hasattr(name, "search") and name.search("I understand"):
            return understand
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role

    _dismiss_privacy_dialog(page)

    assert understand.click_calls == 1
    page.wait_for_timeout.assert_called_once_with(1000)


def test_wait_for_eightfold_form_rechecks_privacy_dialog_after_apply_click():
    from autofill_eightfold import _wait_for_eightfold_form

    page = mock.Mock()
    dismiss_calls: list[str] = []
    cookie_calls: list[str] = []

    with mock.patch(
        "autofill_eightfold._dismiss_cookie_banner",
        side_effect=lambda current_page: cookie_calls.append("cookie"),
    ), mock.patch(
        "autofill_eightfold._dismiss_privacy_dialog",
        side_effect=lambda current_page: dismiss_calls.append("dismiss"),
    ), mock.patch("autofill_eightfold._click_apply_if_needed") as click_apply:
        _wait_for_eightfold_form(page)

    assert cookie_calls == ["cookie", "cookie"]
    assert dismiss_calls == ["dismiss", "dismiss"]
    click_apply.assert_called_once_with(page)


def test_click_apply_if_needed_ignores_hidden_resume_upload_input():
    from autofill_eightfold import _click_apply_if_needed

    class _HiddenFileInput:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def is_visible(self) -> bool:
            return False

    apply_link = _ButtonLocator()
    page = mock.Mock()
    page.locator.return_value = _HiddenFileInput()
    page.wait_for_timeout = mock.Mock()

    def get_by_role(role: str, name=None):
        if role == "textbox":
            return _EmptyLocator()
        if role == "link" and hasattr(name, "search") and name.search("Apply Now"):
            return apply_link
        if role == "button":
            return _EmptyLocator()
        return _EmptyLocator()

    page.get_by_role.side_effect = get_by_role

    _click_apply_if_needed(page)

    assert apply_link.click_calls == 1


def test_detect_eightfold_auth_result_for_sign_in_gate():
    from autofill_eightfold import _detect_eightfold_auth_result

    class _Body:
        def inner_text(self, **kwargs):
            return """
            Sign in
            Email
            Continue
            Sign in using Google
            Create an account
            """

    class _Page:
        url = "https://qualcomm.eightfold.ai/careers/apply?pid=446717098736"

        def locator(self, selector: str):
            assert selector == "body"
            return _Body()

    page = _Page()

    result = _detect_eightfold_auth_result(
        page,
        {
            "job_url": "https://qualcomm.eightfold.ai/careers/apply?pid=446717098736",
            "company": "Qualcomm",
            "job_title": "Senior Product Manager, Mobile Connectivity",
        },
    )

    assert result is not None
    assert result["status"] == "skipped_auth"
    assert result["failure_type"] == "auth_guarded"
    assert result["auth_state"] == "sign_in_gate"
    assert result["auth_scope"] == "eightfold:qualcomm.eightfold.ai"


def test_eightfold_job_closed_reason_detects_no_longer_accepting_applications():
    from autofill_eightfold import _eightfold_job_closed_reason

    reason = _eightfold_job_closed_reason(
        """
        Principal Product Manager, HBM
        No longer accepting applications.
        """
    )

    assert reason == "no longer accepting applications"


def test_eightfold_job_closed_reason_detects_redirected_no_jobs_found_page():
    from autofill_eightfold import _eightfold_job_closed_reason

    reason = _eightfold_job_closed_reason(
        """
        Browse all opportunities
        We didn't find any relevant jobs
        View all jobs
        """
    )

    assert reason == "we didn't find any relevant jobs"


def test_detect_eightfold_job_closed_result_for_closed_page():
    from autofill_eightfold import _detect_eightfold_job_closed_result

    class _Body:
        def inner_text(self, **kwargs):
            return """
            Principal Product Manager, HBM
            No longer accepting applications.
            """

    class _Page:
        url = "https://micron.eightfold.ai/careers/job?pid=123"

        def locator(self, selector: str):
            assert selector == "body"
            return _Body()

    result = _detect_eightfold_job_closed_result(
        _Page(),
        {
            "job_url": "https://micron.eightfold.ai/careers/job?pid=123",
            "company": "Micron",
            "job_title": "Principal Product Manager, HBM",
        },
    )

    assert result is not None
    assert result["status"] == "job_closed"
    assert result["failure_type"] == "job_closed"
    assert "no longer accepting applications" in result["message"].lower()
