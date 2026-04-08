import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _InputItem:
    def __init__(
        self,
        *,
        tag_name: str = "input",
        input_type: str = "text",
        visible: bool = True,
        enabled: bool = True,
        editable: bool = True,
        checked: bool = False,
        click_requires_force: bool = False,
        value: str = "",
        on_click=None,
    ) -> None:
        self.tag_name = tag_name
        self.input_type = input_type
        self.visible = visible
        self.enabled = enabled
        self.editable = editable
        self.checked = checked
        self.click_requires_force = click_requires_force
        self.value = value
        self.on_click = on_click
        self.fill_calls: list[str] = []
        self.click_calls = 0

    def fill(self, value: str) -> None:
        if not self.visible:
            raise RuntimeError("element is not visible")
        if not self.enabled:
            raise RuntimeError("element is not enabled")
        if not self.editable:
            raise RuntimeError("element is not editable")
        self.value = value
        self.fill_calls.append(value)

    def click(self, force: bool = False) -> None:
        if self.click_requires_force and not force:
            raise RuntimeError("subtree intercepts pointer events")
        self.click_calls += 1
        if self.on_click is not None:
            self.on_click()

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def is_editable(self) -> bool:
        return self.visible and self.enabled and self.editable

    def is_checked(self) -> bool:
        return self.checked

    def inner_text(self) -> str:
        return ""

    def input_value(self) -> str:
        return self.value

    def evaluate(self, script: str) -> str:
        assert "tagName" in script
        return self.tag_name

    def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        if not self.visible:
            raise RuntimeError("element is not visible")
        if not self.enabled:
            raise RuntimeError("element is not enabled")
        selected = label if label is not None else value
        if selected is None:
            raise RuntimeError("no option selected")
        self.value = selected

    def get_attribute(self, name: str) -> str | None:
        if name == "type":
            return self.input_type
        return None

    def check(self, force: bool = False) -> None:
        del force
        if not self.visible:
            raise RuntimeError("element is not visible")
        if not self.enabled:
            raise RuntimeError("element is not enabled")
        self.checked = True

    def uncheck(self, force: bool = False) -> None:
        del force
        if not self.visible:
            raise RuntimeError("element is not visible")
        if not self.enabled:
            raise RuntimeError("element is not enabled")
        self.checked = False


class _Locator:
    def __init__(self, items: list[_InputItem] | None = None) -> None:
        self.items = items or []

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> "_Locator":
        return _Locator([self.items[index]])

    @property
    def first(self) -> "_Locator":
        return self.nth(0) if self.items else _Locator()

    def fill(self, value: str) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].fill(value)

    def input_value(self) -> str:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].input_value()

    def evaluate(self, script: str) -> str:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].evaluate(script)

    def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].select_option(label=label, value=value)

    def get_attribute(self, name: str) -> str | None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].get_attribute(name)

    def click(self, force: bool = False) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].click(force=force)

    def is_visible(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_visible()

    def is_enabled(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_enabled()

    def is_editable(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_editable()

    def is_checked(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_checked()

    def inner_text(self) -> str:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].inner_text()

    def scroll_into_view_if_needed(self) -> None:
        return None

    def check(self, force: bool = False) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].check(force=force)

    def uncheck(self, force: bool = False) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].uncheck(force=force)


class _Page:
    def __init__(
        self,
        *,
        email_locator: _Locator,
        password_locator: _Locator,
        sign_in_button_locator: _Locator | None = None,
        error_locator: _Locator | None = None,
    ) -> None:
        self.email_locator = email_locator
        self.password_locator = password_locator
        self.sign_in_button_locator = sign_in_button_locator or _Locator()
        self.error_locator = error_locator or _Locator()

    def locator(self, selector: str) -> _Locator:
        if "input[type='email']" in selector:
            return self.email_locator
        if "input[type='password']" in selector:
            return self.password_locator
        if "button[type='submit']" in selector:
            return self.sign_in_button_locator
        if "[role='alert']" in selector:
            return self.error_locator
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms


class _LabelPage:
    def __init__(self, labeled_items: dict[str, list[_InputItem]]) -> None:
        self.labeled_items = labeled_items

    def get_by_label(self, label_text: str, exact: bool = False) -> _Locator:
        del exact
        return _Locator(self.labeled_items.get(label_text, []))


class _OuterNewsletterScope:
    def __init__(self) -> None:
        self.email = _InputItem()
        self.submit = _InputItem()

    def locator(self, selector: str) -> _Locator:
        if "input[type='email']" in selector:
            return _Locator([self.email])
        if "button[type='submit']" in selector or "input[type='submit']" in selector:
            return _Locator([self.submit])
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Sign up for updates"


class _FrameContainerPage:
    def __init__(self, *, frames: list[object]) -> None:
        self.frames = frames

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return ""


class _DetachedLocator:
    def count(self) -> int:
        raise RuntimeError("Frame was detached")

    def nth(self, index: int):
        del index
        raise RuntimeError("Frame was detached")

    @property
    def first(self):
        raise RuntimeError("Frame was detached")


class _DetachedScope:
    def locator(self, selector: str):
        del selector
        return _DetachedLocator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Enter Your Information"


class _WritablePage:
    def __init__(self, *, url: str, body_text: str, html: str | None = None) -> None:
        self.url = url
        self.body_text = body_text
        self.html = html or body_text

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.body_text

    def content(self) -> str:
        return self.html


class _ProfileDetectionPage:
    def __init__(self, *, body_text: str, url: str = "https://careers.example.com/jobs/123/profile") -> None:
        self.url = url
        self.body_text = body_text

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.body_text

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator()

    @property
    def frames(self) -> list[object]:
        return []


class _NamedButtonPage:
    def __init__(self, button_names: list[str]) -> None:
        self.buttons = {name: _InputItem() for name in button_names}

    def evaluate(self, script: str) -> None:
        assert "window.scrollTo" in script

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def get_by_role(self, role: str, name: str) -> _Locator:
        assert role == "button"
        item = self.buttons.get(name)
        return _Locator([item]) if item is not None else _Locator()

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator()


class _PrefersVisibleSignInFormPage:
    def __init__(self) -> None:
        self.phase = "initial"
        self.sign_in_tab = _InputItem(on_click=self._open_detour)
        self.initial_email = _InputItem()
        self.initial_password = _InputItem()
        self.initial_submit = _InputItem()
        self.detour_email = _InputItem()
        self.detour_password = _InputItem(visible=False)
        self.detour_submit = _InputItem()

    def _open_detour(self) -> None:
        self.phase = "detour"

    def locator(self, selector: str) -> _Locator:
        if "a:has-text('Sign In')" in selector:
            return _Locator([self.sign_in_tab])
        if "input[type='email']" in selector:
            return _Locator([self.initial_email if self.phase == "initial" else self.detour_email])
        if "input[type='password']" in selector:
            return _Locator([self.initial_password if self.phase == "initial" else self.detour_password])
        if "button[type='submit']" in selector:
            return _Locator([self.initial_submit if self.phase == "initial" else self.detour_submit])
        if "[role='alert']" in selector:
            return _Locator()
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms


class _EmailFirstSignInPage:
    def __init__(self) -> None:
        self.phase = "email"
        self.email = _InputItem()
        self.hidden_password_hint = _InputItem(visible=False)
        self.continue_button = _InputItem(on_click=self._advance_to_password)
        self.password = _InputItem()
        self.submit_button = _InputItem()

    def _advance_to_password(self) -> None:
        self.phase = "password"

    def locator(self, selector: str) -> _Locator:
        if "a:has-text('Sign In')" in selector:
            return _Locator()
        if "input[type='email']" in selector:
            return _Locator([self.email]) if self.phase == "email" else _Locator()
        if "input[type='password']" in selector:
            return _Locator([self.hidden_password_hint]) if self.phase == "email" else _Locator([self.password])
        if "button[type='submit']" in selector:
            return _Locator([self.continue_button]) if self.phase == "email" else _Locator([self.submit_button])
        if "[role='alert']" in selector:
            return _Locator()
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms


class _IframeEmailFirstSignInPage(_EmailFirstSignInPage):
    def locator(self, selector: str) -> _Locator:
        if "input[name='css_loginName']" in selector or "#email" in selector:
            return _Locator([self.email]) if self.phase == "email" else _Locator()
        if "#enterEmailSubmitButton" in selector:
            return _Locator([self.continue_button]) if self.phase == "email" else _Locator()
        return super().locator(selector)

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Enter Your Information"


class _ConsentGatedEmailFirstSignInPage:
    def __init__(self) -> None:
        self.phase = "email"
        self.application = False
        self.email = _InputItem()
        self.consent = _InputItem(input_type="checkbox")
        self.next_button = _InputItem(input_type="submit", on_click=self._advance)
        self.password = _InputItem()
        self.submit_button = _InputItem(input_type="submit", on_click=self._submit)

    def _advance(self) -> None:
        if self.consent.checked:
            self.phase = "password"

    def _submit(self) -> None:
        self.application = True

    def locator(self, selector: str) -> _Locator:
        if "a:has-text('Sign In')" in selector:
            return _Locator()
        if "input[type='email']" in selector:
            return _Locator([self.email]) if self.phase == "email" else _Locator()
        if "input[type='password']" in selector:
            return _Locator([self.password]) if self.phase == "password" else _Locator()
        if "#accept_gdpr" in selector:
            return _Locator([self.consent])
        if "input[type='checkbox']" in selector and any(
            token in selector for token in ("gdpr", "privacy", "consent")
        ):
            return _Locator([self.consent])
        if "label[for='accept_gdpr']" in selector:
            return _Locator()
        if "button[type='submit']" in selector or "input[type='submit']" in selector:
            return _Locator([self.next_button]) if self.phase == "email" else _Locator([self.submit_button])
        if "[role='alert']" in selector:
            return _Locator()
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        if self.phase == "email":
            return "Enter Your Information Data Privacy I confirm"
        return "Enter your password"


class _ConsentGatedEmailFirstCreateAccountPage:
    def __init__(self) -> None:
        self.phase = "email"
        self.application = False
        self.email = _InputItem()
        self.consent = _InputItem(input_type="checkbox")
        self.next_button = _InputItem(input_type="submit", on_click=self._advance)
        self.password = _InputItem(input_type="password")
        self.password_confirm = _InputItem(input_type="password")
        self.register_button = _InputItem(input_type="submit", on_click=self._submit)

    def _advance(self) -> None:
        if self.consent.checked:
            self.phase = "details"

    def _submit(self) -> None:
        self.application = True

    def locator(self, selector: str) -> _Locator:
        if "button[type='submit']" in selector or "input[type='submit']" in selector:
            return _Locator([self.next_button]) if self.phase == "email" else _Locator([self.register_button])
        if any(
            marker in selector
            for marker in (
                "a:has-text('Register')",
                "a:has-text('Create Account')",
                "a:has-text('Create an Account')",
                "a:has-text('Sign Up')",
                "[data-tab='register']",
                "#registerTab",
            )
        ):
            return _Locator()
        if "input[type='email']" in selector:
            return _Locator([self.email]) if self.phase == "email" else _Locator()
        if "#accept_gdpr" in selector:
            return _Locator([self.consent])
        if "input[type='checkbox']" in selector and any(
            token in selector for token in ("gdpr", "privacy", "consent")
        ):
            return _Locator([self.consent])
        if "input[type='password']" in selector:
            if self.phase == "details":
                return _Locator([self.password, self.password_confirm])
            return _Locator()
        return _Locator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        if self.phase == "email":
            return "Create Account Data Privacy I confirm"
        return "Create Account Password Confirm Password"


class _ForceClickConsentGatedCreateAccountPage(_ConsentGatedEmailFirstCreateAccountPage):
    def __init__(self) -> None:
        super().__init__()
        self.next_button = _InputItem(input_type="submit", click_requires_force=True, on_click=self._advance)


class _ReadonlyEmailCreateAccountPage(_ConsentGatedEmailFirstCreateAccountPage):
    def __init__(self) -> None:
        super().__init__()
        self.readonly_email = _InputItem(editable=False, value="debug@example.com")
        self.editable_email = _InputItem()

    def locator(self, selector: str) -> _Locator:
        if "input[type='email']" in selector and self.phase == "email":
            return _Locator([self.readonly_email, self.editable_email])
        return super().locator(selector)


class _WrapperShellPage:
    def __init__(self, *, frames: list[object], form_field_count: int = 6) -> None:
        self.frames = frames
        self.url = "https://careers.example.com/jobs/123/login"
        self.form_fields = [_InputItem() for _ in range(form_field_count)]

    def locator(self, selector: str) -> _Locator:
        if selector == "form input, form select, form textarea":
            return _Locator(self.form_fields)
        return _Locator()

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Corporate careers shell"


class _HcaptchaChallengeFrame:
    def __init__(self) -> None:
        self.url = "https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html#frame=challenge"

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator()

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return "Please try again. Verify"


def test_do_sign_in_skips_hidden_password_autofill_hint_and_fills_visible_password_input():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")

    email_item = _InputItem()
    hidden_password_item = _InputItem(visible=False)
    visible_password_item = _InputItem()
    page = _Page(
        email_locator=_Locator([email_item]),
        password_locator=_Locator([hidden_password_item, visible_password_item]),
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        result = mod._do_sign_in(page, "debug@example.com", "debug-password")

    assert result is False
    assert email_item.fill_calls == ["debug@example.com"]
    assert hidden_password_item.fill_calls == []
    assert visible_password_item.fill_calls == ["debug-password"]


def test_do_sign_in_prefers_visible_form_before_clicking_sign_in_tab():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _PrefersVisibleSignInFormPage()

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        result = mod._do_sign_in(page, "debug@example.com", "debug-password")

    assert result is False
    assert page.sign_in_tab.click_calls == 0
    assert page.initial_email.fill_calls == ["debug@example.com"]
    assert page.initial_password.fill_calls == ["debug-password"]
    assert page.initial_submit.click_calls == 1
    assert page.detour_email.fill_calls == []
    assert page.detour_password.fill_calls == []


def test_do_sign_in_handles_email_first_login_flow_before_password_step():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _EmailFirstSignInPage()

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        result = mod._do_sign_in(page, "debug@example.com", "debug-password")

    assert result is False
    assert page.email.fill_calls == ["debug@example.com"]
    assert page.hidden_password_hint.fill_calls == []
    assert page.continue_button.click_calls == 1
    assert page.password.fill_calls == ["debug-password"]
    assert page.submit_button.click_calls == 1


def test_do_sign_in_accepts_privacy_consent_before_email_first_continue_step():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ConsentGatedEmailFirstSignInPage()

    with mock.patch.object(mod, "_is_application_page", side_effect=lambda current_page: current_page.application):
        result = mod._do_sign_in(page, "debug@example.com", "debug-password")

    assert result is True
    assert page.email.fill_calls == ["debug@example.com"]
    assert page.consent.is_checked() is True
    assert page.next_button.click_calls == 1
    assert page.password.fill_calls == ["debug-password"]
    assert page.submit_button.click_calls == 1


def test_do_create_account_accepts_privacy_consent_before_email_first_registration_step():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ConsentGatedEmailFirstCreateAccountPage()

    with (
        mock.patch.object(mod, "_is_application_page", side_effect=lambda current_page: current_page.application),
        mock.patch.object(
            mod,
            "parse_master_resume",
            return_value=SimpleNamespace(first_name="Jerrison", last_name="Li"),
        ),
    ):
        result = mod._do_create_account(page, "debug@example.com", "debug-password")

    assert result is True
    assert page.email.fill_calls == ["debug@example.com"]
    assert page.consent.is_checked() is True
    assert page.next_button.click_calls == 1
    assert page.password.fill_calls == ["debug-password"]
    assert page.password_confirm.fill_calls == ["debug-password"]
    assert page.register_button.click_calls == 1


def test_do_create_account_force_clicks_auth_submit_when_overlay_intercepts_pointer_events():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ForceClickConsentGatedCreateAccountPage()

    with (
        mock.patch.object(mod, "_is_application_page", side_effect=lambda current_page: current_page.application),
        mock.patch.object(
            mod,
            "parse_master_resume",
            return_value=SimpleNamespace(first_name="Jerrison", last_name="Li"),
        ),
    ):
        result = mod._do_create_account(page, "debug@example.com", "debug-password")

    assert result is True
    assert page.next_button.click_calls == 1
    assert page.register_button.click_calls == 1


def test_do_create_account_skips_readonly_prefilled_email_field():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ReadonlyEmailCreateAccountPage()

    with (
        mock.patch.object(mod, "_is_application_page", side_effect=lambda current_page: current_page.application),
        mock.patch.object(
            mod,
            "parse_master_resume",
            return_value=SimpleNamespace(first_name="Jerrison", last_name="Li"),
        ),
    ):
        result = mod._do_create_account(page, "debug@example.com", "debug-password")

    assert result is True
    assert page.readonly_email.fill_calls == []
    assert page.editable_email.fill_calls == ["debug@example.com"]
    assert page.register_button.click_calls == 1


def test_handle_auth_prefers_iframe_auth_scope_over_outer_newsletter_shell():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    newsletter_scope = _OuterNewsletterScope()
    auth_frame = _IframeEmailFirstSignInPage()
    container = _FrameContainerPage(frames=[newsletter_scope, auth_frame])

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        result = mod._handle_auth(container, "debug@example.com", "debug-password")

    assert result is False
    assert newsletter_scope.email.fill_calls == []
    assert newsletter_scope.submit.click_calls == 0
    assert auth_frame.email.fill_calls == ["debug@example.com"]
    assert auth_frame.continue_button.click_calls == 1
    assert auth_frame.password.fill_calls
    assert set(auth_frame.password.fill_calls) == {"debug-password"}
    assert auth_frame.submit_button.click_calls >= 1


def test_handle_auth_ignores_detached_scope_and_uses_next_scope():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    detached_scope = _DetachedScope()
    auth_frame = _IframeEmailFirstSignInPage()
    container = _FrameContainerPage(frames=[detached_scope, auth_frame])

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        result = mod._handle_auth(container, "debug@example.com", "debug-password")

    assert result is False
    assert auth_frame.email.fill_calls == ["debug@example.com"]
    assert auth_frame.submit_button.click_calls >= 1


def test_is_application_page_rejects_wrapper_shell_when_embedded_auth_gate_is_still_active():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    wrapper = _WrapperShellPage(frames=[_ConsentGatedEmailFirstSignInPage()])

    assert mod._is_application_page(wrapper) is False


def test_detect_current_page_treats_embedded_privacy_auth_gate_as_login():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    wrapper = _WrapperShellPage(frames=[_ConsentGatedEmailFirstSignInPage()])

    assert mod._detect_current_page(wrapper) == mod.PAGE_LOGIN


def test_write_auth_outcome_log_can_record_skipped_captcha_submission_result():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        payload = {
            "job_url": "https://careers-example.icims.com/jobs/123/login",
            "company": "ExampleCo",
            "job_title": "Senior PM",
            "candidate_email": "debug@example.com",
        }
        page = _WritablePage(
            url="https://careers-example.icims.com/jobs/123/login",
            body_text="Please click on the shape that is different",
        )

        mod._write_auth_outcome_log(
            out_dir,
            payload,
            page,
            detail_status="captcha_required",
            message="iCIMS authentication is blocked by a captcha challenge before sign in can continue.",
            suggestions=["Solve the captcha in a headed browser and rerun the canonical output dir."],
            submission_status="skipped_captcha",
            submission_failure_type="skipped_captcha",
            auth_state="captcha_required",
        )

        auth_failure = json.loads((out_dir / "submit" / "icims_auth_failure.json").read_text(encoding="utf-8"))
        result = json.loads((out_dir / "submit" / "application_submission_result.json").read_text(encoding="utf-8"))
        assert auth_failure["auth_scope"] == "icims:careers-example.icims.com"
        assert result["status"] == "skipped_captcha"
        assert result["failure_type"] == "skipped_captcha"
        assert result["auth_state"] == "captcha_required"
        assert result["auth_scope"] == "icims:careers-example.icims.com"
        assert "captcha challenge" in result["message"].lower()


def test_classify_icims_auth_blocker_recognizes_cloudfront_service_unavailable():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _WritablePage(
        url="https://careers.example.com/jobs/123/login",
        body_text="502 ERROR\nThe request could not be satisfied.\nGenerated by cloudfront (CloudFront)",
    )

    result = mod._classify_icims_auth_blocker(page, {"hcaptcha_challenge_active": False})

    assert result["detail_status"] == "service_unavailable"
    assert result["submission_status"] == "service_unavailable"
    assert result["submission_failure_type"] == "service_unavailable"


def test_classify_icims_auth_blocker_recognizes_not_found_search_page_as_job_closed():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _WritablePage(
        url=(
            "https://careers-ddn.icims.com/jobs/search?ss=1&notFound=1&mobile=false"
            "&width=1360&height=500&bga=true&needsRedirect=false"
        ),
        body_text=(
            "Join the DDN Team\nYou\nSupercharging AI Data Workloads\nABOUT US\nCareers\n"
            "Contact A Storage Specialist\nOffice Locations"
        ),
    )

    result = mod._classify_icims_auth_blocker(page, {"hcaptcha_challenge_active": False})

    assert result["detail_status"] == "job_closed"
    assert result["submission_status"] == "job_closed"
    assert result["submission_failure_type"] == "job_closed"
    assert "job_closed" in result["message"]


def test_classify_icims_auth_blocker_recognizes_hcaptcha_challenge_in_embedded_frame():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _WrapperShellPage(frames=[_ConsentGatedEmailFirstSignInPage(), _HcaptchaChallengeFrame()], form_field_count=0)

    result = mod._classify_icims_auth_blocker(page, {"hcaptcha_challenge_active": False})

    assert result["detail_status"] == "captcha_required"
    assert result["submission_status"] == "skipped_captcha"
    assert result["submission_failure_type"] == "skipped_captcha"


def test_detect_current_page_treats_candidate_profile_variant_as_profile():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ProfileDetectionPage(
        body_text="Candidate Profile Resume Cover Letter First Name Last Name Phone Additional Data"
    )

    with mock.patch.object(mod, "_has_login_indicators", return_value=False):
        assert mod._detect_current_page(page) == mod.PAGE_PROFILE


def test_detect_current_page_prefers_profile_when_candidate_profile_contains_demographic_keywords():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _ProfileDetectionPage(
        body_text=(
            "Candidate Profile Resume Cover Letter First Name Last Name Phone Additional Data "
            "Please answer the demographic questions below Gender Identity Veteran Status Disability"
        )
    )

    with mock.patch.object(mod, "_has_login_indicators", return_value=False):
        assert mod._detect_current_page(page) == mod.PAGE_PROFILE


def test_active_application_scope_prefers_embedded_candidate_profile_frame():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    outer = _OuterNewsletterScope()
    profile_frame = _ProfileDetectionPage(
        body_text="Candidate Profile Resume Cover Letter First Name Last Name Phone Additional Data"
    )
    container = _FrameContainerPage(frames=[outer, profile_frame])

    assert mod._active_application_scope(container) is profile_frame


def test_click_next_button_accepts_submit_profile_variant():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    page = _NamedButtonPage(["Submit Profile"])

    assert mod._click_next_button(page) is True
    assert page.buttons["Submit Profile"].click_calls == 1


def test_fill_profile_page_populates_visible_login_credentials():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    profile = SimpleNamespace(
        first_name="Jerrison",
        last_name="Li",
        email="jerrisonli@gmail.com",
        phone="+1 415-555-0100",
        location="San Francisco, CA",
        linkedin="https://linkedin.com/in/jerrisonli",
    )
    application_profile = SimpleNamespace(
        street_address="",
        location="San Francisco, CA",
        zip_code="94105",
        linkedin="https://linkedin.com/in/jerrisonli",
    )

    def fake_fill_by_label(page, label_text, value):
        del page, value
        return label_text in {"Login", "Password", "Password (Re-enter)"}

    with (
        mock.patch.object(mod, "find_resume_file", side_effect=FileNotFoundError()),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError()),
        mock.patch.object(mod, "_fill_by_label", side_effect=fake_fill_by_label),
        mock.patch.object(mod, "_fill_text_field", return_value=False),
    ):
        filled = mod._fill_profile_page(
            _FrameContainerPage(frames=[]),
            profile,
            application_profile,
            Path("/tmp"),
            login_email="jerrisonli@gmail.com",
            login_password="debug-password",
        )

    assert {entry["field_name"] for entry in filled} >= {
        "login",
        "login_password",
        "login_password_reenter",
    }


def test_fill_profile_step_merges_profile_and_deterministic_embedded_questions():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    profile_fills = [{"field_name": "first_name", "filled": True}]
    embedded_question_fills = [{"field_name": "family_relationship", "filled": True}]

    with (
        mock.patch.object(mod, "_fill_profile_page", return_value=profile_fills) as fill_profile,
        mock.patch.object(mod, "_fill_application_questions", return_value=embedded_question_fills) as fill_questions,
    ):
        result = mod._fill_profile_step(
            page=object(),
            profile=object(),
            application_profile=object(),
            out_dir=Path("/tmp"),
            meta={"company": "Joby Aviation"},
            provider="openai",
            login_email="jerrisonli@gmail.com",
            login_password="debug-password",
        )

    assert result == profile_fills + embedded_question_fills
    fill_profile.assert_called_once()
    fill_questions.assert_called_once()
    assert fill_questions.call_args.kwargs["allow_generated"] is False


def test_fill_by_label_prefers_visible_enabled_field_over_hidden_duplicate():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    hidden = _InputItem(visible=False)
    visible = _InputItem()
    page = _LabelPage({"Password": [hidden, visible]})

    result = mod._fill_by_label(page, "Password", "debug-password")

    assert result is True
    assert hidden.fill_calls == []
    assert visible.fill_calls == ["debug-password"]


def test_answer_from_classifier_prefers_current_city_option_for_hybrid_location_prompt():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    answer, source = mod._answer_from_classifier(
        "This is a Hybrid position in San Francisco. Please select which applies:",
        profile,
        kind="select",
        options=[
            "You currently live in San Francisco",
            "You are looking to relocate to San Francisco",
        ],
    )

    assert answer == "You currently live in San Francisco"
    assert source == "shared_positive_fit_policy"
