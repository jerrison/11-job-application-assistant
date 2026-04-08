import base64
import importlib.util
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unpadded_base64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class _DummyElement:
    def __init__(self, text: str = "", *, visible: bool = True, aria_label: str | None = None) -> None:
        self.text = text
        self.visible = visible
        self.aria_label = aria_label
        self.click_calls = 0

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        if name == "aria-label":
            return self.aria_label
        return None

    def scroll_into_view_if_needed(self) -> None:
        return None

    def click(self, timeout: int | None = None, force: bool = False) -> None:
        self.click_calls += 1


class _DummyLocator:
    def __init__(
        self, elements: list[_DummyElement] | None = None, children: dict[str, "_DummyLocator"] | None = None
    ) -> None:
        self.elements = elements or []
        self.children = children or {}

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _DummyElement:
        return self.elements[index]

    @property
    def first(self) -> _DummyElement:
        return self.nth(0)

    def locator(self, selector: str) -> "_DummyLocator":
        return self.children.get(selector, _DummyLocator())


class _DummyPage:
    def __init__(
        self,
        *,
        locators: dict[str, _DummyLocator] | None = None,
        role_locators: dict[tuple[str, str], _DummyLocator] | None = None,
        body_text: str = "",
    ) -> None:
        self.locators = locators or {}
        self.role_locators = role_locators or {}
        self.body_text = body_text

    def locator(self, selector: str) -> _DummyLocator:
        return self.locators.get(selector, _DummyLocator())

    def get_by_role(self, role: str, name: str) -> _DummyLocator:
        return self.role_locators.get((role, name), _DummyLocator())

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.body_text

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _ActionItem:
    def __init__(
        self,
        *,
        visible: bool = True,
        checked: bool = False,
        enabled: bool = True,
        fail_when_hidden: bool = False,
    ) -> None:
        self.visible = visible
        self.checked = checked
        self.enabled = enabled
        self.fail_when_hidden = fail_when_hidden
        self.fill_calls: list[str] = []
        self.click_calls = 0
        self.check_calls = 0

    def fill(self, value: str) -> None:
        if self.fail_when_hidden and not self.visible:
            raise RuntimeError("element is not visible")
        self.fill_calls.append(value)

    def click(self, force: bool = False) -> None:
        self.click_calls += 1

    def check(self) -> None:
        self.checked = True
        self.check_calls += 1

    def is_checked(self) -> bool:
        return self.checked

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled


class _ActionLocator:
    def __init__(self, items: list[_ActionItem] | None = None) -> None:
        self.items = items or []

    @classmethod
    def single(cls, item: _ActionItem | None = None) -> "_ActionLocator":
        return cls([] if item is None else [item])

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> "_ActionLocator":
        return _ActionLocator([self.items[index]])

    @property
    def first(self) -> "_ActionLocator":
        return self.nth(0) if self.items else _ActionLocator()

    def fill(self, value: str) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].fill(value)

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del timeout
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].click(force=force)

    def check(self) -> None:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        self.items[0].check()

    def is_checked(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_checked()

    def is_visible(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_visible()

    def is_enabled(self) -> bool:
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return self.items[0].is_enabled()


class _ActionPage:
    def __init__(
        self,
        *,
        locators: dict[str, _ActionLocator] | None = None,
        role_locators: dict[tuple[str, str], _ActionLocator] | None = None,
        url: str = "https://example.test/apply/applyManually",
    ) -> None:
        self.locators = locators or {}
        self.role_locators = role_locators or {}
        self.url = url
        self.goto_calls: list[str] = []

    def locator(self, selector: str) -> _ActionLocator:
        return self.locators.get(selector, _ActionLocator())

    def get_by_role(self, role: str, name: str, exact: bool | None = None) -> _ActionLocator:
        del exact
        return self.role_locators.get((role, name), _ActionLocator())

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        del wait_until, timeout
        self.goto_calls.append(url)
        self.url = url

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _ToggleActionItem(_ActionItem):
    def __init__(self, on_click, **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_click = on_click

    def click(self, force: bool = False) -> None:
        super().click(force=force)
        self._on_click()


class _EmailEntrypointAuthPage:
    def __init__(self) -> None:
        self.email_mode = False
        self.create_account_mode = False
        self.url = "https://example.test/login"
        self.email = _ActionItem()
        self.password = _ActionItem()
        self.verify_password = _ActionItem()
        self.sign_in_with_email = _ToggleActionItem(self._open_email_mode)
        self.sign_in_button = _ActionItem()
        self.forgot_button = _ActionItem()
        self.create_account_link = _ToggleActionItem(self._open_create_account_mode)

    def _open_email_mode(self) -> None:
        self.email_mode = True

    def _open_create_account_mode(self) -> None:
        self.email_mode = True
        self.create_account_mode = True

    def locator(self, selector: str) -> _ActionLocator:
        if selector == "dialog, [role='dialog']":
            return _ActionLocator()
        if selector == "button:has-text('Sign in with email'), a:has-text('Sign in with email')" and not self.email_mode:
            return _ActionLocator.single(self.sign_in_with_email)
        if selector == "a:has-text('Create Account'), button:has-text('Create Account')" and self.email_mode:
            return _ActionLocator.single(self.create_account_link)
        if selector == "button:has-text('Forgot'), a:has-text('Forgot')" and self.email_mode:
            return _ActionLocator.single(self.forgot_button)
        if selector == "[data-automation-id='email']" and self.email_mode:
            return _ActionLocator.single(self.email)
        if selector == "[data-automation-id='password']" and self.email_mode:
            return _ActionLocator.single(self.password)
        if selector == "[data-automation-id='verifyPassword']" and self.create_account_mode:
            return _ActionLocator.single(self.verify_password)
        return _ActionLocator()

    def get_by_role(self, role: str, name: str, exact: bool | None = None) -> _ActionLocator:
        del exact
        if role == "button" and name == "Sign in with email" and not self.email_mode:
            return _ActionLocator.single(self.sign_in_with_email)
        if role == "button" and name == "Sign In" and self.email_mode:
            return _ActionLocator.single(self.sign_in_button)
        if role == "button" and name == "Forgot your password" and self.email_mode:
            return _ActionLocator.single(self.forgot_button)
        if role == "button" and name == "Create Account" and self.email_mode:
            return _ActionLocator.single(self.create_account_link)
        if role == "textbox" and name == "Email Address" and self.email_mode:
            return _ActionLocator.single(self.email)
        if role == "textbox" and name == "Password" and self.email_mode:
            return _ActionLocator.single(self.password)
        if role == "textbox" and name == "Verify New Password" and self.create_account_mode:
            return _ActionLocator.single(self.verify_password)
        return _ActionLocator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        del wait_until, timeout
        self.url = url

    def go_back(self) -> None:
        return None


class _WorkdayPromptInput:
    def __init__(self, *, fail_on_empty_fill: bool = False) -> None:
        self.fail_on_empty_fill = fail_on_empty_fill
        self.fill_calls: list[str] = []
        self.type_calls: list[str] = []
        self.click_calls = 0
        self.focus_calls = 0
        self.press_calls: list[str] = []

    def count(self) -> int:
        return 1

    @property
    def first(self):
        return self

    def click(self, timeout: int | None = None, force: bool = False) -> None:
        del timeout, force
        self.click_calls += 1

    def focus(self) -> None:
        self.focus_calls += 1

    def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        if self.fail_on_empty_fill and value == "":
            raise RuntimeError("clear failed")

    def type(self, value: str, delay: int = 0) -> None:
        del delay
        self.type_calls.append(value)

    def press(self, key: str) -> None:
        self.press_calls.append(key)


class _WorkdayPromptOption:
    def __init__(self, text: str) -> None:
        self.text = text
        self.click_calls = 0

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self.text

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del force, timeout
        self.click_calls += 1


class _WorkdayPromptOptions:
    def __init__(self, options: list[_WorkdayPromptOption]) -> None:
        self.options = options

    def filter(self, *, has_text: str):
        return _WorkdayPromptOptions([option for option in self.options if has_text in option.text])

    def count(self) -> int:
        return len(self.options)

    def nth(self, index: int) -> _WorkdayPromptOption:
        return self.options[index]


class _WorkdayPromptPage:
    def __init__(self, input_locator: _WorkdayPromptInput, option_locator: _WorkdayPromptOptions) -> None:
        self.input_locator = input_locator
        self.option_locator = option_locator

    def locator(self, selector: str):
        if selector.startswith("input"):
            return self.input_locator
        if any(
            fragment in selector
            for fragment in (
                "[data-automation-id='menuItem']",
                "[role='option']",
                "[data-automation-id='promptOption']",
            )
        ):
            return self.option_locator
        return self.input_locator

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _KeyboardRecorder:
    def __init__(self) -> None:
        self.press_calls: list[str] = []

    def press(self, key: str) -> None:
        self.press_calls.append(key)


class _PromptOptionItem:
    def __init__(
        self,
        text: str,
        *,
        data_automation_id: str = "menuItem",
        role: str = "option",
        selected_chip: bool = False,
        visible: bool = True,
        on_click=None,
    ) -> None:
        self.text = text
        self.data_automation_id = data_automation_id
        self.role = role
        self.selected_chip = selected_chip
        self.visible = visible
        self.click_calls = 0
        self._on_click = on_click

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self) -> str:
        return self.text

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del force, timeout
        self.click_calls += 1
        if self._on_click is not None:
            self._on_click()

    def get_attribute(self, name: str) -> str | None:
        if name == "data-automation-id":
            return self.data_automation_id
        if name == "role":
            return self.role
        return None

    def evaluate(self, script: str) -> bool:
        del script
        return self.selected_chip


class _DropdownFieldLocator:
    def __init__(self, *, value: str = "", field_id: str | None = None) -> None:
        self.value = value
        self.field_id = field_id or ""
        self.fill_calls: list[str] = []
        self.click_calls = 0

    def count(self) -> int:
        return 1

    @property
    def first(self):
        return self

    def input_value(self) -> str:
        return self.value

    def fill(self, value: str) -> None:
        self.value = value
        self.fill_calls.append(value)

    def scroll_into_view_if_needed(self) -> None:
        return None

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del force, timeout
        self.click_calls += 1

    def inner_text(self) -> str:
        return self.value

    def get_attribute(self, name: str) -> str | None:
        if name == "id":
            return self.field_id
        if name == "aria-label":
            return self.value
        return None


class _PromptOptionsPage:
    def __init__(
        self,
        *,
        input_locator: _WorkdayPromptInput | None = None,
        option_batches: list[list[_PromptOptionItem]] | None = None,
        dropdown: _DropdownFieldLocator | None = None,
        input_selector: str = '[id="education-1--fieldOfStudy"]',
    ) -> None:
        self.input_locator = input_locator or _WorkdayPromptInput()
        self.option_batches = option_batches or [[]]
        self.dropdown = dropdown
        self.input_selector = input_selector
        self.keyboard = _KeyboardRecorder()

    def locator(self, selector: str):
        if selector == self.input_selector:
            return self.input_locator
        if "menuItem" in selector or "role='option'" in selector or "promptOption" in selector or ".css-option" in selector:
            batch_index = 0
            if self.dropdown is not None and self.dropdown.click_calls:
                batch_index = min(self.dropdown.click_calls - 1, len(self.option_batches) - 1)
            return _LocatorCollection(self.option_batches[batch_index])
        return _EmptyLocator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _PromptStatePage:
    def __init__(self) -> None:
        self.keyboard = _KeyboardRecorder()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _FieldLocator:
    def __init__(self, *, value: str = "", field_id: str | None = None) -> None:
        self.value = value
        self.field_id = field_id or ""
        self.fill_calls: list[str] = []
        self.click_calls = 0

    def count(self) -> int:
        return 1

    @property
    def first(self):
        return self

    def input_value(self) -> str:
        return self.value

    def fill(self, value: str) -> None:
        self.value = value
        self.fill_calls.append(value)

    def scroll_into_view_if_needed(self) -> None:
        return None

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del force, timeout
        self.click_calls += 1

    def inner_text(self) -> str:
        return self.value

    def get_attribute(self, name: str) -> str | None:
        if name == "id":
            return self.field_id
        if name == "aria-label":
            return self.value
        return None


class _CheckboxLocator(_FieldLocator):
    def __init__(self, *, checked: bool = False, field_id: str | None = None) -> None:
        super().__init__(value="", field_id=field_id)
        self.checked = checked
        self.check_calls = 0

    def is_checked(self) -> bool:
        return self.checked

    def check(self, force: bool = False) -> None:
        del force
        self.checked = True
        self.check_calls += 1


class _VisibilityFieldLocator(_FieldLocator):
    def __init__(self, *, value: str = "", field_id: str | None = None, visible: bool = True) -> None:
        super().__init__(value=value, field_id=field_id)
        self.visible = visible

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return True


class _EmptyLocator:
    def count(self) -> int:
        return 0

    @property
    def first(self):
        return self

    def nth(self, index: int):
        del index
        return self


class _LocatorCollection:
    def __init__(self, items: list[object] | None = None) -> None:
        self.items = items or []

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]

    @property
    def first(self):
        return self.nth(0)


class _ClickableItem:
    def __init__(self, on_click=None, *, visible: bool = True, enabled: bool = True) -> None:
        self._on_click = on_click
        self.visible = visible
        self.enabled = enabled
        self.click_calls = 0

    def click(self, force: bool = False, timeout: int | None = None) -> None:
        del force, timeout
        self.click_calls += 1
        if self._on_click is not None:
            self._on_click()

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def scroll_into_view_if_needed(self) -> None:
        return None


class _WorkExperienceRow:
    def __init__(
        self,
        *,
        job_title: _FieldLocator,
        company: _FieldLocator,
        location: _FieldLocator,
        current_checkbox: _CheckboxLocator,
        start_month: _FieldLocator,
        start_year: _FieldLocator,
        role_description: _FieldLocator,
        end_month: _FieldLocator | None = None,
        end_year: _FieldLocator | None = None,
    ) -> None:
        self.locators = {
            "[data-fkit-id$='--jobTitle'] input": job_title,
            "[data-fkit-id$='--companyName'] input": company,
            "[data-fkit-id$='--location'] input": location,
            "[data-fkit-id$='--currentlyWorkHere'] input[type='checkbox']": current_checkbox,
            "[data-fkit-id$='--startDate'] input[aria-label='Month']": start_month,
            "[data-fkit-id$='--startDate'] input[aria-label='Year']": start_year,
            "[data-fkit-id$='--roleDescription'] textarea": role_description,
            "[data-fkit-id$='--endDate'] input[aria-label='Month']": end_month or _EmptyLocator(),
            "[data-fkit-id$='--endDate'] input[aria-label='Year']": end_year or _EmptyLocator(),
        }

    def locator(self, selector: str):
        return self.locators.get(selector, _EmptyLocator())


class _EducationRow:
    def __init__(
        self,
        *,
        school: _FieldLocator,
        degree: _FieldLocator,
        field_of_study: _FieldLocator,
        first_year: _FieldLocator,
        last_year: _FieldLocator,
    ) -> None:
        self.locators = {
            "[data-fkit-id$='--schoolName'] input": school,
            "[data-fkit-id$='--degree'] button": degree,
            "[data-fkit-id$='--fieldOfStudy'] input": field_of_study,
            "[data-fkit-id$='--firstYearAttended'] input[aria-label='Year']": first_year,
            "[data-fkit-id$='--lastYearAttended'] input[aria-label='Year']": last_year,
        }

    def locator(self, selector: str):
        return self.locators.get(selector, _EmptyLocator())


class _MyExperiencePage:
    def __init__(
        self,
        education_rows: list[_EducationRow],
        *,
        work_experience_rows: list[_WorkExperienceRow] | None = None,
        selector_items: dict[str, list[object]] | None = None,
    ) -> None:
        self._education_rows = education_rows
        self._work_experience_rows = work_experience_rows or []
        self._selector_items = selector_items or {}

    def locator(self, selector: str):
        if selector == "input[type='file'][data-automation-id='file-upload-input-ref'], input[type='file']":
            return _EmptyLocator()
        if selector == "input[type='file']":
            return _LocatorCollection()
        if selector == "[data-fkit-id^='workExperience-'][data-fkit-id$='--null']":
            return _LocatorCollection(self._work_experience_rows)
        if selector == "[data-fkit-id^='education-'][data-fkit-id$='--null']":
            return _LocatorCollection(self._education_rows)
        if selector in self._selector_items:
            return _LocatorCollection(self._selector_items[selector])
        return _EmptyLocator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class _EmptyPage:
    def locator(self, selector: str):
        del selector
        return _EmptyLocator()

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


_FACTSET_PASSWORD_RESET_MARKERS = {
    "page_url": "https://factset.wd108.myworkdayjobs.com/en-US/FactSetCareers/passwordReset",
    "page_text_excerpt": (
        "Reset Password Enter your email address to reset your password. Verification Code Reset Password"
    ),
}

_FACTSET_SIGN_IN_MARKERS = {
    "page_url": "https://factset.wd108.myworkdayjobs.com/en-US/FactSetCareers/login",
    "page_text_excerpt": (
        "FactSet Careers page is loaded Sign In Email Address Password Sign In Create Account Forgot your password?"
    ),
    "alert_text": "",
    "heading_text": "FactSet Careers",
    "visible_actions": ["Sign In", "Create Account", "Forgot your password?"],
}

_SNAP_WORKDAY_MAINTENANCE_MARKERS = {
    "page_url": "https://community.workday.com/maintenance-page",
    "page_text_excerpt": (
        "Workday is currently unavailable. We are experiencing a service interruption. Please check back later."
    ),
}

_ALATION_PASSWORD_RESET_MARKERS = {
    "page_url": "https://alation.wd5.myworkdayjobs.com/en-US/Alation/passwordReset",
    "page_text_excerpt": ("Forgot your password? Enter the verification code from your email to reset password."),
}


def test_workday_preferred_locator_narrows_single_match_to_stable_locator():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    raw_locator = _ActionLocator.single(_ActionItem())

    selected = mod._workday_preferred_locator(raw_locator)

    assert selected is not raw_locator
    assert selected.count() == 1


def test_select_workday_prompt_option_via_input_tolerates_clear_failure():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    input_locator = _WorkdayPromptInput(fail_on_empty_fill=True)
    option = _WorkdayPromptOption("LinkedIn")
    page = _WorkdayPromptPage(input_locator, _WorkdayPromptOptions([option]))

    with mock.patch.object(mod, "_workday_prompt_selection_matches", return_value=True):
        result = mod._select_workday_prompt_option_via_input(
            page,
            "input[data-automation-id='promptOption']",
            "LinkedIn",
            label_text="How Did You Hear About Us?",
        )

    assert result is True
    assert input_locator.type_calls == ["LinkedIn"]
    assert option.click_calls == 1


def test_workday_prompt_selection_matches_ignores_visible_option_text_when_selected_value_differs():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-source">
      <label for="source--source">
        <span>How Did You Hear About Us?<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div data-automation-id="multiselectInputContainer">
          <input
            id="source--source"
            data-uxi-multiselect-id="multi-source"
            autocomplete="off"
            value=""
          />
          <div data-automation-id="promptAriaInstruction">1 item selected, Adobe MAX</div>
        </div>
        <ul data-automation-id="selectedItemList">
          <li data-automation-id="selectedItem">
            <p data-automation-id="promptOption">Adobe MAX</p>
          </li>
        </ul>
        <div data-automation-id="options-root">
          <div data-automation-id="menuItem" data-uxi-multiselect-id="multi-source" role="option">
            <p data-automation-id="promptOption">Adobe.com</p>
          </div>
          <div data-automation-id="menuItem" data-uxi-multiselect-id="multi-source" role="option">
            <p data-automation-id="promptOption">Adobe MAX</p>
          </div>
        </div>
      </div>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert mod._workday_prompt_selection_matches(page, "How Did You Hear About Us?", "Adobe.com") is False
        assert mod._workday_prompt_selection_matches(page, "How Did You Hear About Us?", "Adobe MAX") is True

        browser.close()


def test_workday_prompt_selection_matches_uses_prompt_instruction_when_selected_chip_is_missing():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    with mock.patch.object(
        mod,
        "_workday_prompt_state_for_label",
        return_value={
            "fieldText": "",
            "inputValue": "",
            "promptInstruction": "1 item selected, Job Boards",
            "selectedItems": [],
            "visibleOptions": [],
            "highlightedOptions": [],
        },
    ):
        assert mod._workday_prompt_selection_matches(mock.Mock(), "How Did You Hear About Us?", "Job Board") is True


def test_workday_prompt_selection_matches_treats_source_job_board_variant_as_match():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    with mock.patch.object(
        mod,
        "_workday_prompt_state_for_label",
        return_value={
            "fieldText": "",
            "inputValue": "",
            "promptInstruction": "1 item selected, Job Board - Other",
            "selectedItems": ["Job Board - Other"],
            "visibleOptions": [],
            "highlightedOptions": [],
        },
    ):
        assert mod._workday_prompt_selection_matches(mock.Mock(), "How Did You Hear About Us?", "Job Board") is True
        assert mod._workday_prompt_selection_matches(mock.Mock(), "How Did You Hear About Us?", "TrueUp") is False


def test_clear_workday_prompt_selection_removes_selected_chip():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-source">
      <label for="source--source">
        <span>How Did You Hear About Us?<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div data-automation-id="multiselectInputContainer">
          <input
            id="source--source"
            data-uxi-multiselect-id="multi-source"
            autocomplete="off"
            value=""
          />
          <div data-automation-id="promptAriaInstruction">1 item selected, Adobe Career Academy</div>
        </div>
        <ul data-automation-id="selectedItemList">
          <li data-automation-id="menuItem">
            <div data-automation-id="selectedItem" title="Adobe Career Academy">
              <div data-automation-id="DELETE_charm"></div>
              <p data-automation-id="promptOption">Adobe Career Academy</p>
            </div>
          </li>
        </ul>
      </div>
    </div>
    <script>
      const deleteCharm = document.querySelector('[data-automation-id="DELETE_charm"]');
      const selectedItemList = document.querySelector('[data-automation-id="selectedItemList"]');
      const instruction = document.querySelector('[data-automation-id="promptAriaInstruction"]');
      deleteCharm.addEventListener('click', () => {
        selectedItemList.innerHTML = '';
        instruction.textContent = '0 items selected';
      });
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert mod._clear_workday_prompt_selection(page, "How Did You Hear About Us?") is True
        assert mod._workday_prompt_selection_matches(page, "How Did You Hear About Us?", "Adobe Career Academy") is False

        browser.close()


def test_workday_email_field_prefers_exact_applicant_email_over_referral_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-referredBy">
      <label for="source--referredBy">What's their name or email address?</label>
      <input id="source--referredBy" type="text" value="" />
    </div>
    <div data-automation-id="formField-email">
      <label for="candidate-email">Email Address</label>
      <input id="candidate-email" type="text" value="" />
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        field = mod._workday_email_field(page, "Email", "Email Address")
        field.fill("candidate@example.test")

        assert page.locator("#candidate-email").input_value() == "candidate@example.test"
        assert page.locator("#source--referredBy").input_value() == ""

        browser.close()


def test_fill_workday_labeled_dropdown_supports_digit_led_field_ids():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = object()
    with (
        mock.patch.object(mod, "_workday_field_id_for_label", return_value="123abc-field"),
        mock.patch.object(mod, "_fill_workday_dropdown", return_value=True) as fill_dropdown,
    ):
        result = mod._fill_workday_labeled_dropdown(page, "School", "Stanford University")

    assert result is True
    assert fill_dropdown.call_args.args[1] == '[id="123abc-field"]'


def test_workday_field_id_for_label_normalizes_question_mark_against_colon_suffix():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-hispanicOrLatino">
      <label for="personalInfoUS--hispanicOrLatino">
        <span>Hispanic or Latino:<abbr aria-hidden="true">*</abbr></span>
      </label>
      <button id="personalInfoUS--hispanicOrLatino" type="button">Select One</button>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert mod._workday_field_id_for_label(page, "Hispanic or Latino?") == "personalInfoUS--hispanicOrLatino"

        browser.close()


def test_select_workday_checkbox_option_supports_digit_led_input_ids():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div role="grid">
      <fieldset>
        <div role="row">
          <div role="cell">
            <div class="css-1utp272">
              <div class="css-d3pjdr">
                <input
                  aria-checked="false"
                  aria-required="true"
                  class="css-1r7y3ml"
                  id="123abc-option"
                  type="checkbox"
                />
                <span class="css-15ws53q"></span>
                <div class="css-1ikf28c"><div class="css-wwg2k6"></div></div>
              </div>
              <label class="css-1ew7hmu" cursor="pointer" for="123abc-option">
                I have not worked for Adobe in the past.
              </label>
            </div>
          </div>
        </div>
      </fieldset>
    </div>
    <script>
      const checkbox = document.getElementById("123abc-option");
      const indicator = document.querySelector(".css-1ikf28c > div");
      checkbox.addEventListener("change", () => {
        indicator.className = checkbox.checked ? "css-checked" : "css-wwg2k6";
      });
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert (
            mod._select_workday_checkbox_option(page, "I have not worked for Adobe in the past.")
            is True
        )
        assert page.locator('[id="123abc-option"]').is_checked() is True

        browser.close()


def test_fill_workday_labeled_checkbox_group_uses_truthful_decline_for_split_ethnicity_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    label_text = "Please select the ethnicity (or ethnicities) which most accurately describe(s) how you identify yourself."
    html = f"""
    <div data-automation-id="formField-ethnicityMulti">
      <fieldset aria-invalid="true" id="personalInfoUS--ethnicityMulti">
        <legend>
          <label id="label47">{label_text}</label>
        </legend>
        <div role="grid">
          <div role="row">
            <div role="cell">
              <input id="white-option" type="checkbox" />
              <label for="white-option">White (United States of America)</label>
            </div>
          </div>
          <div role="row">
            <div role="cell">
              <input id="decline-option" type="checkbox" />
              <label for="decline-option">I do not wish to answer. (United States of America)</label>
            </div>
          </div>
        </div>
      </fieldset>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        selected = mod._fill_workday_labeled_checkbox_group(
            page,
            label_text,
            "Hispanic or Latino",
            profile_field="race_or_ethnicity",
        )

        assert selected == "I do not wish to answer. (United States of America)"
        assert page.locator('[id="decline-option"]').is_checked() is True

        browser.close()


def test_fill_workday_labeled_checkbox_group_accepts_plain_value_group_label_and_declares_truthful_decline():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    label_text = "Please select the appropriate value(s)"
    html = f"""
    <div data-automation-id="formField-ethnicityMulti">
      <fieldset aria-invalid="true" id="personalInfoUS--ethnicityMulti">
        <legend>
          <label id="label47">{label_text}</label>
        </legend>
        <div role="grid">
          <div role="row">
            <div role="cell">
              <input id="asian-option" type="checkbox" />
              <label for="asian-option">Asian (United States of America)</label>
            </div>
          </div>
          <div role="row">
            <div role="cell">
              <input id="decline-option" type="checkbox" />
              <label for="decline-option">Do not wish to declare (United States of America)</label>
            </div>
          </div>
        </div>
      </fieldset>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        selected = mod._fill_workday_labeled_checkbox_group_candidates(
            page,
            mod._WORKDAY_RACE_LABEL_CANDIDATES,
            "Hispanic or Latino",
            profile_field="race_or_ethnicity",
        )

        assert selected == "Do not wish to declare (United States of America)"
        assert page.locator('[id="decline-option"]').is_checked() is True
        assert page.locator('[id="asian-option"]').is_checked() is False

        browser.close()


def test_try_radio_or_checkbox_prefers_exact_hispanic_match_over_negated_option_labels():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div>
      <input id="american-indian" type="checkbox" />
      <label for="american-indian">
        American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)
      </label>
      <input id="hispanic" type="checkbox" />
      <label for="hispanic">Hispanic or Latino (United States of America)</label>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        filled: list[dict] = []
        mod._try_radio_or_checkbox(page, "race", "Hispanic or Latino", filled)

        assert page.locator("#american-indian").is_checked() is False
        assert page.locator("#hispanic").is_checked() is True
        assert filled == [
            {
                "field_name": "race",
                "value": "Hispanic or Latino (United States of America)",
                "source": "application_profile.md",
                "filled": True,
            }
        ]

        browser.close()


def test_fill_workday_source_prompt_selects_company_website_leaf_option():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-source">
      <label for="source--source">
        <span>How Did You Hear About Us?<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div data-automation-id="multiselectInputContainer">
          <input
            id="source--source"
            data-uxi-multiselect-id="multi-source"
            autocomplete="off"
            value=""
          />
          <div data-automation-id="promptAriaInstruction">0 items selected</div>
        </div>
        <ul data-automation-id="selectedItemList"></ul>
        <div data-automation-id="options-root" hidden>
          <div data-automation-id="menuItem" data-uxi-multiselect-id="multi-source" role="option">
            <p data-automation-id="promptOption">Adobe.com</p>
          </div>
          <div data-automation-id="menuItem" data-uxi-multiselect-id="multi-source" role="option">
            <p data-automation-id="promptOption">Job Boards</p>
          </div>
        </div>
      </div>
    </div>
    <script>
      const input = document.getElementById("source--source");
      const optionsRoot = document.querySelector('[data-automation-id="options-root"]');
      const selectedItemList = document.querySelector('[data-automation-id="selectedItemList"]');
      const instruction = document.querySelector('[data-automation-id="promptAriaInstruction"]');

      const openOptions = () => {
        optionsRoot.hidden = false;
      };

      input.addEventListener("focus", openOptions);
      input.addEventListener("click", openOptions);
      input.addEventListener("input", () => {
        const needle = input.value.toLowerCase();
        for (const option of optionsRoot.querySelectorAll('[data-automation-id="menuItem"]')) {
          const text = option.textContent.trim().toLowerCase();
          option.hidden = Boolean(needle) && !text.includes(needle);
        }
      });

      for (const option of optionsRoot.querySelectorAll('[data-automation-id="menuItem"]')) {
        option.addEventListener("click", () => {
          const text = option.textContent.trim();
          selectedItemList.innerHTML = `
            <li data-automation-id="selectedItem">
              <p data-automation-id="promptOption">${text}</p>
            </li>
          `;
          instruction.textContent = `1 item selected, ${text}`;
          input.value = "";
          optionsRoot.hidden = true;
        });
      }
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        selected = mod._fill_workday_source_prompt(
            page,
            "How Did You Hear About Us?",
            ["Adobe.com", "Corporate website"],
        )

        assert selected == "Adobe.com"
        assert page.locator('[data-automation-id="selectedItem"] [data-automation-id="promptOption"]').inner_text() == (
            "Adobe.com"
        )

        browser.close()


def test_fill_workday_source_prompt_rejects_mismatched_committed_value():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = mock.Mock()

    with (
        mock.patch.object(mod, "_workday_field_id_for_label", return_value="source--source"),
        mock.patch.object(mod, "_select_workday_prompt_option_via_input", return_value=True),
        mock.patch.object(
            mod,
            "_workday_prompt_state_for_label",
            return_value={
                "fieldText": "",
                "inputValue": "",
                "promptInstruction": "1 item selected, Adobe MAX",
                "selectedItems": ["Adobe MAX"],
                "visibleOptions": ["Adobe.com", "Adobe MAX"],
                "highlightedOptions": [],
            },
        ),
    ):
        selected = mod._fill_workday_source_prompt(
            page,
            "How Did You Hear About Us?",
            ["Adobe.com"],
        )

    assert selected is None


def test_fill_workday_source_prompt_reuses_matching_job_board_variant_without_reselection():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = mock.Mock()

    with (
        mock.patch.object(mod, "_workday_field_id_for_label", return_value="source--source"),
        mock.patch.object(
            mod,
            "_workday_prompt_state_for_label",
            return_value={
                "fieldText": "",
                "inputValue": "",
                "promptInstruction": "1 item selected, Job Board - Other",
                "selectedItems": ["Job Board - Other"],
                "visibleOptions": ["Job Board - Other"],
                "highlightedOptions": [],
            },
        ),
        mock.patch.object(mod, "_select_workday_prompt_option_via_input", return_value=False) as select_prompt,
    ):
        selected = mod._fill_workday_source_prompt(
            page,
            "How Did You Hear About Us?",
            ["TrueUp", "Job Board"],
        )

    assert selected == "Job Board - Other"
    select_prompt.assert_not_called()


def test_fill_workday_labeled_radio_selects_requested_value_and_verifies_group_state():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-candidateIsPreviousWorker">
      <fieldset aria-invalid="true" id="previous-worker-fieldset">
        <legend>
          <label id="radio-label8">
            <span>Have you been employed by Adobe in the past?<abbr aria-hidden="true">*</abbr></span>
          </label>
        </legend>
        <div id="previousWorker--candidateIsPreviousWorker" aria-invalid="true">
          <div class="css-1utp272">
            <input id="previous-worker-yes" name="candidateIsPreviousWorker" type="radio" value="true" />
            <label for="previous-worker-yes">Yes</label>
          </div>
          <div class="css-1utp272">
            <input id="previous-worker-no" name="candidateIsPreviousWorker" type="radio" value="false" />
            <label for="previous-worker-no">No</label>
          </div>
        </div>
      </fieldset>
    </div>
    <script>
      const fieldset = document.getElementById("previous-worker-fieldset");
      const group = document.getElementById("previousWorker--candidateIsPreviousWorker");
      const inputs = Array.from(document.querySelectorAll('input[name="candidateIsPreviousWorker"]'));

      for (const label of document.querySelectorAll('label[for]')) {
        label.addEventListener("click", (event) => {
          const input = document.getElementById(label.getAttribute("for"));
          for (const other of inputs) {
            other.checked = other === input;
          }
          fieldset.setAttribute("aria-invalid", "false");
          group.setAttribute("aria-invalid", "false");
          event.preventDefault();
        });
      }
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        filled = mod._fill_workday_labeled_radio(
            page,
            "Have you been employed by Adobe in the past?",
            "No",
        )

        assert filled is True
        assert page.locator("#previous-worker-no").is_checked() is True
        assert page.locator("#previous-worker-fieldset").get_attribute("aria-invalid") == "false"

        browser.close()


def test_fill_workday_labeled_radio_falls_back_to_input_click_when_label_click_is_intercepted():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-candidateIsPreviousWorker">
      <fieldset aria-invalid="true" id="previous-worker-fieldset">
        <legend>
          <label id="radio-label8">
            <span>Have you been employed by Adobe in the past?<abbr aria-hidden="true">*</abbr></span>
          </label>
        </legend>
        <div id="previousWorker--candidateIsPreviousWorker" aria-invalid="true">
          <div class="css-1utp272">
            <input id="previous-worker-yes" name="candidateIsPreviousWorker" type="radio" value="true" />
            <label for="previous-worker-yes">Yes</label>
          </div>
          <div class="css-1utp272">
            <input id="previous-worker-no" name="candidateIsPreviousWorker" type="radio" value="false" />
            <label for="previous-worker-no">No</label>
          </div>
        </div>
      </fieldset>
    </div>
    <script>
      const fieldset = document.getElementById("previous-worker-fieldset");
      const group = document.getElementById("previousWorker--candidateIsPreviousWorker");
      const yesInput = document.getElementById("previous-worker-yes");
      const noInput = document.getElementById("previous-worker-no");
      const noLabel = document.querySelector('label[for="previous-worker-no"]');

      noLabel.addEventListener("click", (event) => {
        event.preventDefault();
      });

      noInput.addEventListener("click", () => {
        yesInput.checked = false;
        noInput.checked = true;
        fieldset.setAttribute("aria-invalid", "false");
        group.setAttribute("aria-invalid", "false");
      });
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        filled = mod._fill_workday_labeled_radio(
            page,
            "Have you been employed by Adobe in the past?",
            "No",
        )

        assert filled is True
        assert page.locator("#previous-worker-no").is_checked() is True
        assert page.locator("#previous-worker-fieldset").get_attribute("aria-invalid") == "false"

        browser.close()


def test_fill_voluntary_disclosures_accepts_select_your_veteran_status_label():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = SimpleNamespace(
        race_or_ethnicity="",
        gender="",
        veteran_status="I am not a veteran",
        disability_status="",
    )
    attempted_labels: list[str] = []

    def _fake_fill(_page, label_text: str, value: str, *, profile_field: str | None = None) -> bool:
        del value
        del profile_field
        attempted_labels.append(label_text)
        return label_text == "Please select your veteran status."

    with (
        mock.patch.object(mod, "_fill_workday_labeled_dropdown", side_effect=_fake_fill),
        mock.patch.object(mod, "_try_radio_or_checkbox", return_value=None),
        mock.patch.object(mod, "_check_workday_checkbox_for_label", return_value=False),
    ):
        filled = mod._fill_voluntary_disclosures(_EmptyPage(), application_profile)

    assert "Please select your veteran status." in attempted_labels
    assert any(entry["field_name"] == "veteran_status" for entry in filled)


def test_fill_voluntary_disclosures_checks_generic_confirmation_checkbox():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = SimpleNamespace(
        race_or_ethnicity="",
        gender="",
        veteran_status="",
        disability_status="",
    )
    attempted_labels: list[str] = []

    def _fake_check(_page, label_text: str) -> bool:
        attempted_labels.append(label_text)
        return "confirm the statement above" in label_text.casefold()

    with (
        mock.patch.object(mod, "_fill_workday_labeled_dropdown", return_value=False),
        mock.patch.object(mod, "_try_radio_or_checkbox", return_value=None),
        mock.patch.object(mod, "_check_workday_checkbox_for_label", side_effect=_fake_check),
    ):
        filled = mod._fill_voluntary_disclosures(_EmptyPage(), application_profile)

    assert any("confirm the statement above" in label.casefold() for label in attempted_labels)
    assert any(entry["field_name"] == "required_acknowledgment_checked" for entry in filled)


def test_fill_voluntary_disclosures_accepts_plain_workday_demographic_labels():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = SimpleNamespace(
        race_or_ethnicity="Hispanic or Latino",
        gender="Cisgender Male/Man",
        veteran_status="I am not a protected veteran",
        disability_status="",
    )
    attempted_calls: list[tuple[tuple[str, ...], str, str | None]] = []

    def _fake_fill(_page, label_candidates: tuple[str, ...], value: str, *, profile_field: str | None = None) -> bool:
        attempted_calls.append((label_candidates, value, profile_field))
        return False

    with (
        mock.patch.object(mod, "_fill_workday_labeled_dropdown", return_value=False),
        mock.patch.object(mod, "_fill_workday_labeled_dropdown_candidates", side_effect=_fake_fill),
        mock.patch.object(mod, "_try_radio_or_checkbox", return_value=None),
        mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=False),
    ):
        mod._fill_voluntary_disclosures(_EmptyPage(), application_profile)

    assert any("What is your gender?" in labels for labels, _, _ in attempted_calls)
    assert any("What is your race?" in labels for labels, _, _ in attempted_calls)
    assert any("Race/Ethnicity" in labels for labels, _, _ in attempted_calls)
    assert any(
        "Please select the ethnicity (or ethnicities) which most accurately describe(s) how you identify yourself."
        in labels
        for labels, _, _ in attempted_calls
    )
    assert any("Are you a U.S. Veteran?" in labels for labels, _, _ in attempted_calls)


def test_fill_voluntary_disclosures_accepts_hpe_colon_suffixed_demographic_labels():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = SimpleNamespace(
        race_or_ethnicity="Hispanic or Latino",
        gender="",
        veteran_status="I am not a protected veteran",
        disability_status="",
    )
    html = """
    <div data-automation-id="formField-hispanicOrLatino">
      <label for="personalInfoUS--hispanicOrLatino">
        <span>Hispanic or Latino:<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div style="width: 100%; max-width: 344px; min-width: 280px;">
          <div class="css-12zup1l">
            <button
              aria-haspopup="listbox"
              type="button"
              value=""
              aria-label="Hispanic or Latino: Select One Required"
              name="hispanicOrLatino"
              id="personalInfoUS--hispanicOrLatino"
              class="css-3ffqxp"
            >Select One</button>
          </div>
          <div class="workday-options" hidden>
            <div data-automation-id="menuItem" role="option">Yes</div>
            <div data-automation-id="menuItem" role="option">No</div>
          </div>
        </div>
      </div>
    </div>
    <div data-automation-id="formField-ethnicity">
      <label for="personalInfoUS--ethnicity">
        <span>Ethnicity:<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div style="width: 100%; max-width: 344px; min-width: 280px;">
          <div class="css-12zup1l">
            <button
              aria-haspopup="listbox"
              type="button"
              value=""
              aria-label="Ethnicity: Select One Required"
              name="ethnicity"
              id="personalInfoUS--ethnicity"
              class="css-3ffqxp"
            >Select One</button>
          </div>
          <div class="workday-options" hidden>
            <div data-automation-id="menuItem" role="option">Hispanic or Latino</div>
            <div data-automation-id="menuItem" role="option">White</div>
          </div>
        </div>
      </div>
    </div>
    <div data-automation-id="formField-veteranStatus">
      <label for="personalInfoUS--veteranStatus">
        <span>Veterans Status:<abbr aria-hidden="true">*</abbr></span>
      </label>
      <div class="css-15rz5ap">
        <div style="width: 100%; max-width: 344px; min-width: 280px;">
          <div class="css-12zup1l">
            <button
              aria-haspopup="listbox"
              type="button"
              value=""
              aria-label="Veterans Status: Select One Required"
              name="veteranStatus"
              id="personalInfoUS--veteranStatus"
              class="css-3ffqxp"
            >Select One</button>
          </div>
          <div class="workday-options" hidden>
            <div data-automation-id="menuItem" role="option">I am a veteran</div>
            <div data-automation-id="menuItem" role="option">I am not a veteran</div>
          </div>
        </div>
      </div>
    </div>
    <script>
      const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
      for (const field of document.querySelectorAll('[data-automation-id^="formField-"]')) {
        const button = field.querySelector("button");
        const options = field.querySelector(".workday-options");
        const label = normalize(field.querySelector("label")?.textContent || "");
        if (!button || !options) continue;
        button.addEventListener("click", () => {
          for (const other of document.querySelectorAll(".workday-options")) {
            other.hidden = other !== options;
          }
          options.hidden = false;
        });
        for (const option of options.querySelectorAll('[data-automation-id="menuItem"]')) {
          option.addEventListener("click", () => {
            const text = normalize(option.textContent || "");
            button.textContent = text;
            button.setAttribute("aria-label", `${label} ${text}`);
            options.hidden = true;
          });
        }
      }
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        filled = mod._fill_voluntary_disclosures(page, application_profile)
        filled_by_field = {entry["field_name"]: entry["value"] for entry in filled}

        assert page.locator("#personalInfoUS--hispanicOrLatino").inner_text() == "Yes"
        assert page.locator("#personalInfoUS--ethnicity").inner_text() == "Hispanic or Latino"
        assert page.locator("#personalInfoUS--veteranStatus").inner_text() == "I am not a veteran"
        assert filled_by_field["hispanic_or_latino"] == "Yes"
        assert filled_by_field["race_ethnicity"] == "Hispanic or Latino"
        assert "veteran_status" in filled_by_field

        browser.close()


def test_advance_workday_prompt_highlighted_selection_advances_to_nested_options_with_enter():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = _PromptStatePage()
    top_level = {
        "fieldText": "How Did You Hear About Us? Expanded",
        "inputValue": "Adobe Source",
        "promptInstruction": "Expanded",
        "selectedItems": [],
        "visibleOptions": ["Adobe Source", "Job Board"],
        "highlightedOptions": ["Adobe Source"],
    }
    nested_level = {
        "fieldText": "How Did You Hear About Us? Expanded",
        "inputValue": "Adobe Source",
        "promptInstruction": "Expanded",
        "selectedItems": [],
        "visibleOptions": ["Adobe.com", "Adobe MAX"],
        "highlightedOptions": ["Adobe.com"],
    }

    with mock.patch.object(mod, "_workday_prompt_state_for_label", side_effect=[top_level, nested_level]):
        state = mod._advance_workday_prompt_highlighted_selection(page, "How Did You Hear About Us?")

    assert page.keyboard.press_calls == ["Enter"]
    assert state["visibleOptions"] == ["Adobe.com", "Adobe MAX"]
    assert state["highlightedOptions"] == ["Adobe.com"]


def test_advance_workday_prompt_highlighted_selection_falls_back_to_space_when_enter_keeps_state():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = _PromptStatePage()
    unchanged = {
        "fieldText": "How Did You Hear About Us? Expanded",
        "inputValue": "Adobe.com",
        "promptInstruction": "Expanded",
        "selectedItems": [],
        "visibleOptions": ["Adobe.com"],
        "highlightedOptions": ["Adobe.com"],
    }
    committed = {
        "fieldText": "How Did You Hear About Us? 1 item selected, Adobe.com",
        "inputValue": "",
        "promptInstruction": "1 item selected, Adobe.com",
        "selectedItems": ["Adobe.com"],
        "visibleOptions": ["Adobe.com"],
        "highlightedOptions": [],
    }

    with mock.patch.object(mod, "_workday_prompt_state_for_label", side_effect=[unchanged, unchanged, committed]):
        state = mod._advance_workday_prompt_highlighted_selection(page, "How Did You Hear About Us?")

    assert page.keyboard.press_calls == ["Enter", "Space"]
    assert state["selectedItems"] == ["Adobe.com"]


def test_classifies_factset_password_reset_markers_as_password_reset_gate():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url=_FACTSET_PASSWORD_RESET_MARKERS["page_url"],
        page_text=_FACTSET_PASSWORD_RESET_MARKERS["page_text_excerpt"],
    )

    assert result["auth_state"] == "password_reset_gate"


def test_classifies_factset_sign_in_markers_as_sign_in_gate():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url=_FACTSET_SIGN_IN_MARKERS["page_url"],
        page_text=_FACTSET_SIGN_IN_MARKERS["page_text_excerpt"],
        alert_text=_FACTSET_SIGN_IN_MARKERS["alert_text"],
        heading_text=_FACTSET_SIGN_IN_MARKERS["heading_text"],
        visible_actions=_FACTSET_SIGN_IN_MARKERS["visible_actions"],
    )

    assert result["auth_state"] == "sign_in_gate"


def test_classifies_plain_login_shell_with_create_account_link_as_sign_in_gate():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://turo.wd12.myworkdayjobs.com/en-US/Turo_careers/login",
        page_text=(
            "Turo Careers page is loaded Sign In Email Address* Password* Sign In "
            "Don't have an account yet? Create Account Forgot your password?"
        ),
        heading_text="Turo Careers",
        visible_actions=[
            "Home",
            "Search for Jobs",
            "Sign In",
            "Create Account",
            "Forgot your password?",
        ],
    )

    assert result["auth_state"] == "sign_in_gate"


def test_classifies_explicit_missing_page_shell_as_job_unavailable():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/example",
        page_text="Skip to main content English Sign In Search for Jobs The page you are looking for doesn't exist.",
        visible_actions=["Sign In", "Search for Jobs"],
        alert_text="The page you are looking for doesn't exist.",
    )

    assert result["auth_state"] == "job_unavailable"


def test_classifies_account_verification_gate_from_sign_in_shell():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://calix.wd1.myworkdayjobs.com/en-US/External/login",
        page_text=(
            "Sign In Verify your account before you sign in or request a verification email. "
            "Resend Account Verification Create Account Forgot your password?"
        ),
        heading_text="Sign In",
        alert_text="Verify your account before you sign in or request a verification email.",
        visible_actions=["Sign In", "Resend Account Verification", "Create Account", "Forgot your password?"],
    )

    assert result["auth_state"] == "account_verification_gate"


def test_classifies_maintenance_from_snap_markers():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url=_SNAP_WORKDAY_MAINTENANCE_MARKERS["page_url"],
        page_text=_SNAP_WORKDAY_MAINTENANCE_MARKERS["page_text_excerpt"],
    )

    assert result["auth_state"] == "maintenance"


def test_classifies_password_reset_gate_from_alation_markers():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url=_ALATION_PASSWORD_RESET_MARKERS["page_url"],
        page_text=_ALATION_PASSWORD_RESET_MARKERS["page_text_excerpt"],
    )

    assert result["auth_state"] == "password_reset_gate"


def test_classifies_explicit_credential_rejection_from_alert_text():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://factset.wd108.myworkdayjobs.com/FactSetCareers/login",
        page_text="Sign In Email Address Password Sign In Forgot your password?",
        alert_text="Invalid email or password. Please try again.",
    )

    assert result["auth_state"] == "credential_rejected"


def test_classifies_walmart_wrong_email_or_password_variant_as_credential_rejected():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/login",
        page_text="Sign In Email Address Password Sign In Create Account Forgot your password?",
        alert_text="You may have entered the wrong email address or password or your account might be locked.",
    )

    assert result["auth_state"] == "credential_rejected"


def test_classifies_authenticated_non_form_from_user_home_url():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://factset.wd108.myworkdayjobs.com/en-US/userHome",
        page_text="Candidate Home Job Alerts Sign Out",
    )

    assert result["auth_state"] == "authenticated_non_form"


def test_classifies_authenticated_non_form_from_candidate_home_markers_without_canonical_url():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    result = mod._classify_workday_auth_state(
        page_url="https://hpe.wd5.myworkdayjobs.com/en-US/Careers",
        page_text="Candidate Home My Applications My Tasks Search for Jobs Job Alerts Sign Out",
        heading_text="Candidate Home",
        visible_actions=["My Applications", "Search for Jobs", "Sign Out"],
    )

    assert result["auth_state"] == "authenticated_non_form"


def test_fetch_workday_email_link_raises_immediately_on_gws_auth_failure():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    with mock.patch.object(
        mod,
        "_run_gws_json",
        side_effect=RuntimeError("gws CLI failed: invalid_grant: Token has been expired or revoked."),
    ):
        with pytest.raises(RuntimeError, match="invalid_grant"):
            mod._fetch_workday_email_link(
                ["from:otp.workday.com reset password"],
                mod.re.compile(r"https://example.com/reset"),
                wait_seconds=0,
            )


def test_fetch_workday_verification_code_raises_immediately_on_gws_auth_failure():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    with mock.patch.object(
        mod,
        "_run_gws_json",
        side_effect=RuntimeError("gws CLI failed: authError: Token has been expired or revoked."),
    ):
        with pytest.raises(RuntimeError, match="authError"):
            mod._fetch_workday_verification_code(wait_seconds=0)


def test_extract_email_body_decodes_padded_nested_html_parts():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    bare_link = "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
    tokenized_link = (
        "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
        "token123/?redirect=%2Fen-US%2FServiceTitan%2Fjob%2FUS-Remote%2FSenior-Product-Manager_JR113230"
        "%2Fapply%2FapplyManually"
    )
    message = {
        "snippet": f"Click this link to confirm your email address {bare_link}",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _unpadded_base64url(f"Click this link to confirm your email address {bare_link}")},
                },
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {
                                "data": _unpadded_base64url(
                                    f"<html><body>Click this link to confirm your email address {tokenized_link}</body></html>"
                                )
                            },
                        }
                    ],
                },
            ],
        },
    }

    assert tokenized_link in mod._extract_email_body(message)


def test_select_workday_account_verification_link_prefers_tokenized_redirect_for_current_job():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    preferred_job_url = (
        "https://servicetitan.wd1.myworkdayjobs.com/en-US/ServiceTitan/job/US-Remote/"
        "Senior-Product-Manager_JR113230"
    )
    bare_link = "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
    tokenized_link = (
        "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
        "token123/?redirect=%2Fen-US%2FServiceTitan%2Fjob%2FUS-Remote%2FSenior-Product-Manager_JR113230"
        "%2Fapply%2FapplyManually"
    )
    other_tenant_link = (
        "https://costar.wd1.myworkdayjobs.com/CoStar/job/US-Remote/"
        "Principal-Product-Manager/apply/applyManually"
    )
    body = f"{bare_link} {tokenized_link} {other_tenant_link}"

    assert (
        mod._select_workday_account_verification_link(body, preferred_job_url=preferred_job_url) == tokenized_link
    )


def test_fetch_workday_account_verification_link_falls_back_to_recent_matching_email():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    preferred_job_url = (
        "https://servicetitan.wd1.myworkdayjobs.com/en-US/ServiceTitan/job/US-Remote/"
        "Senior-Product-Manager_JR113230"
    )
    bare_link = "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
    tokenized_link = (
        "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan/activate/"
        "token123/?redirect=%2Fen-US%2FServiceTitan%2Fjob%2FUS-Remote%2FSenior-Product-Manager_JR113230"
        "%2Fapply%2FapplyManually"
    )
    verification_started_at = datetime(2026, 4, 5, 8, 0, tzinfo=UTC)
    recent_message = {
        "snippet": f"Click this link to confirm your email address {bare_link}",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {
                        "data": _unpadded_base64url(
                            f"<html><body>Click this link to confirm your email address {tokenized_link}</body></html>"
                        )
                    },
                }
            ],
        },
    }
    list_queries: list[str] = []

    def fake_run_gws_json(args: list[str]) -> dict:
        if args[:4] == ["gmail", "users", "messages", "list"]:
            query = json.loads(args[-1])["q"]
            list_queries.append(query)
            if query.startswith("after:"):
                return {}
            if query == "newer_than:1d from:otp.workday.com":
                return {"messages": [{"id": "msg-1"}]}
            raise AssertionError(f"Unexpected list query: {query}")
        if args[:4] == ["gmail", "users", "messages", "get"]:
            return recent_message
        raise AssertionError(f"Unexpected gws args: {args}")

    with mock.patch.object(mod, "_run_gws_json", side_effect=fake_run_gws_json):
        link = mod._fetch_workday_account_verification_link(
            min_received_at_utc=verification_started_at,
            wait_seconds=0,
            preferred_job_url=preferred_job_url,
        )

    assert link == tokenized_link
    assert list_queries[0].startswith("after:")
    assert list_queries[1] == "newer_than:1d from:otp.workday.com"


def test_workday_auth_scope_derives_tenant_for_myworkdayjobs():
    mod = load_module("job_board_urls", "scripts/job_board_urls.py")

    assert (
        mod.workday_auth_scope(
            "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/United-States-Boston/"
            "Senior-Product-Manager---Performance-Solutions_R30990"
        )
        == "workday:factset/factsetcareers"
    )


def test_workday_auth_scope_derives_tenant_for_myworkdaysite():
    mod = load_module("job_board_urls", "scripts/job_board_urls.py")

    assert (
        mod.workday_auth_scope(
            "https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/"
            "Palo-Alto-California/Principal-Product-Manager--Ads-Platform_R0044575-1"
        )
        == "workday:snapchat/snap"
    )


def test_build_auth_result_uses_unknown_status_without_explicit_rejection():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    payload = {
        "job_url": "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/example",
        "company": "FactSet",
        "job_title": "Senior PM",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": "https://factset.wd108.myworkdayjobs.com/en-US/FactSetCareers/job/example/apply/applyManually",
        "page_text_excerpt": "Create Account Sign In Forgot your password?",
        "heading_text": "Create Account",
        "alert_text": "",
        "visible_actions": ["Create Account", "Sign In", "Forgot your password?"],
        "auth_state": "create_account_gate",
    }

    result = mod._build_workday_auth_result(
        payload,
        markers,
        auth_scope="workday:factset/factsetcareers",
        last_attempted_step="create_account",
        credential_rejection_observed=False,
    )

    assert result["status"] == "auth_unknown"
    assert result["auth_state"] == "create_account_gate"
    assert result["auth_scope"] == "workday:factset/factsetcareers"
    assert result["last_attempted_step"] == "create_account"
    assert result["retryable"] is False


def test_build_auth_result_marks_maintenance_retryable():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    payload = {
        "job_url": "https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/example",
        "company": "Snap Inc.",
        "job_title": "Principal Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": "https://community.workday.com/maintenance-page",
        "page_text_excerpt": "Workday is currently unavailable. We are experiencing a service interruption.",
        "heading_text": "Workday is currently unavailable",
        "alert_text": "",
        "visible_actions": [],
        "auth_state": "maintenance",
    }

    result = mod._build_workday_auth_result(
        payload,
        markers,
        auth_scope="workday:snapchat/snap",
        last_attempted_step="sign_in",
        credential_rejection_observed=False,
    )

    assert result["status"] == "service_unavailable"
    assert result["retryable"] is True
    assert "unavailable" in result["message"].lower()


def test_build_auth_result_marks_missing_page_as_job_closed():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    payload = {
        "job_url": "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/example",
        "company": "Adobe",
        "job_title": "Principal Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": payload["job_url"],
        "page_text_excerpt": (
            "Skip to main content English Sign In Search for Jobs "
            "The page you are looking for doesn't exist."
        ),
        "heading_text": "",
        "alert_text": "The page you are looking for doesn't exist.",
        "visible_actions": ["Sign In", "Search for Jobs"],
        "auth_state": "sign_in_gate",
    }

    result = mod._build_workday_auth_result(
        payload,
        markers,
        auth_scope="workday:adobe/external-experienced",
        last_attempted_step="create_account",
        credential_rejection_observed=False,
    )

    assert result["status"] == "job_closed"
    assert result["auth_state"] == "job_unavailable"
    assert result["retryable"] is False
    assert "job_closed:" in result["message"]


def test_build_auth_result_preserves_auth_state_hint_when_final_page_is_unknown():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    payload = {
        "job_url": "https://liveramp.wd5.myworkdayjobs.com/LiveRampCareers/job/example",
        "company": "LiveRamp",
        "job_title": "Lead Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": "chrome-error://chromewebdata/",
        "page_text_excerpt": "This page isn't working. HTTP ERROR 406.",
        "heading_text": "This page isn't working",
        "alert_text": "Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://liveramp.wd5.myworkdayjobs.com/",
        "visible_actions": ["Reload"],
        "auth_state": "unknown",
    }

    result = mod._build_workday_auth_result(
        payload,
        markers,
        auth_scope="workday:liveramp/liverampcareers",
        last_attempted_step="create_account",
        credential_rejection_observed=False,
        auth_state_hint="account_verification_gate",
    )

    assert result["status"] == "auth_unknown"
    assert result["auth_state"] == "account_verification_gate"
    assert "HTTP_RESPONSE_CODE_FAILURE" in result["alert_text"]


def test_preferred_workday_source_option_prefers_job_boards_for_how_did_you_hear():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Adobe Source", "Job Boards", "Social Media"],
    )

    assert selected == "Job Boards"


def test_preferred_workday_source_option_prefers_company_source_for_company_website_profile():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Adobe Source", "Job Board", "Social Media"],
        company_name="ADUS-Adobe Inc.",
        source_answer="Corporate website",
    )

    assert selected == "Adobe Source"


def test_preferred_workday_source_option_prefers_job_board_when_metadata_source_is_trueup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Adobe Source", "Job Board", "Social Media"],
        source_hint="https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert selected == "Job Board"


def test_preferred_workday_source_option_prefers_company_domain_leaf_for_company_website_profile():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Adobe.com", "Adobe Career Academy", "Adobe Recruiting Team"],
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert selected == "Adobe.com"


def test_preferred_workday_source_option_prefers_other_job_board_for_trueup_followup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "Source",
        ["Employee Referral", "Other Job Board", "LinkedIn"],
        source_hint="https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert selected == "Other Job Board"


def test_preferred_workday_source_option_prefers_other_for_trueup_primary_prompt_when_only_other_and_linkedin_exist():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["LinkedIn", "Other"],
        source_hint="https://autodesk.wd1.myworkdayjobs.com/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="Autodesk Inc.",
    )

    assert selected == "Other"


def test_preferred_workday_source_option_prefers_job_board_over_company_domain_for_trueup_primary_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Adobe.com", "Adobe Source", "Job Board", "Social Media"],
        source_hint="https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert selected == "Job Board"


def test_preferred_workday_source_option_prefers_social_networking_for_linkedin_sourced_primary_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "How Did You Hear About Us?",
        ["Job Board", "Social Networking", "Other"],
        source_hint="linkedin",
    )

    assert selected == "Social Networking"


def test_workday_source_search_candidates_try_exact_leaf_before_social_fallback():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    candidates = mod._workday_source_search_candidates(
        "How Did You Hear About Us?",
        ["Job Board", "Social Networking", "Other"],
        source_hint="linkedin",
    )

    assert candidates[:2] == ["LinkedIn", "Social Networking"]


def test_workday_source_search_candidates_include_raw_preferences_when_options_missing():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    candidates = mod._workday_source_search_candidates(
        "How Did You Hear About Us?",
        [],
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert "Adobe Source" in candidates


def test_workday_source_search_candidates_include_company_domain_leaf_as_trueup_fallback():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    candidates = mod._workday_source_search_candidates(
        "How Did You Hear About Us?",
        ["Adobe.com", "Adobe Source", "Job Board", "Social Media"],
        source_hint="https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert candidates[:4] == ["Job Board", "Adobe.com", "Adobe Source", "Social Media"]


def test_workday_source_search_candidates_keep_raw_trueup_search_terms_when_visible_options_are_partial():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    candidates = mod._workday_source_search_candidates(
        "How Did You Hear About Us?",
        ["Adobe Source", "Job Board", "Social Media"],
        source_hint="https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_answer="Corporate website",
        company_name="ADUS-Adobe Inc.",
    )

    assert "TrueUp" in candidates


def test_workday_source_hint_from_payload_uses_board_url_when_direct_source_fields_are_blank():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    payload = {
        "job_source": "",
        "source": "",
        "source_url": "",
        "board_url": "https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        "job_url": "https://adobe.wd5.myworkdayjobs.com/en-US/external/job/example",
    }

    assert mod._workday_source_hint_from_payload(payload) == payload["board_url"].casefold()


def test_preferred_workday_source_option_prefers_career_site_for_company_website_source_followup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "Source",
        ["Career Site", "Employee Referral", "LinkedIn"],
        source_answer="Corporate website",
    )

    assert selected == "Career Site"


def test_preferred_workday_source_option_prefers_linkedin_for_source_followup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    selected = mod._preferred_workday_source_option(
        "Source",
        ["Built In", "Indeed", "LinkedIn", "ZipRecruiter"],
    )

    assert selected == "LinkedIn"


def test_run_workday_auth_flow_returns_auth_result_when_create_account_step_raises():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://calix.wd1.myworkdayjobs.com/External/job/example",
        "company": "Calix",
        "job_title": "Senior Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": payload["job_url"],
        "page_text_excerpt": "Create Account Sign In Forgot your password?",
        "heading_text": "Create Account",
        "alert_text": "",
        "visible_actions": ["Create Account", "Sign In", "Forgot your password?"],
        "auth_state": "create_account_gate",
    }

    with mock.patch.object(mod, "_ensure_workday_application_context", return_value=False):
        with mock.patch.object(mod, "_extract_workday_auth_markers", return_value=markers):
            with mock.patch.object(mod, "_open_workday_sign_in"):
                with mock.patch.object(mod, "_open_workday_create_account"):
                    with mock.patch.object(mod, "_do_sign_in", return_value=False):
                        with mock.patch.object(mod, "_do_password_reset", return_value=False):
                            with mock.patch.object(mod, "_do_create_account", side_effect=RuntimeError("fill failed")):
                                result = mod._run_workday_auth_flow(
                                    page,
                                    "jerrisonli@gmail.com",
                                    "password",
                                    payload=payload,
                                    job_url=payload["job_url"],
                                    out_dir=None,
                                    enter_application_flow=False,
                                )

    assert result["ok"] is False
    assert result["result"]["status"] == "auth_unknown"
    assert result["result"]["auth_state"] == "create_account_gate"
    assert result["result"]["last_attempted_step"] == "create_account"


def test_run_workday_auth_flow_marks_post_create_account_credential_rejection_as_auth_failed():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/example",
        "company": "Walmart Inc.",
        "job_title": "Principal, Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    sign_in_markers = {
        "page_url": "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/login",
        "page_text_excerpt": "Sign In Email Address Password Sign In Create Account Forgot your password?",
        "heading_text": "Sign In",
        "alert_text": "",
        "visible_actions": ["Sign In", "Create Account", "Forgot your password?"],
        "auth_state": "sign_in_gate",
    }
    rejected_markers = {
        "page_url": "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/login",
        "page_text_excerpt": (
            "Sign In You may have entered the wrong email address or password or your account might be locked. "
            "Email Address Password Sign In Create Account Forgot your password?"
        ),
        "heading_text": "Sign In",
        "alert_text": "You may have entered the wrong email address or password or your account might be locked.",
        "visible_actions": ["Sign In", "Create Account", "Forgot your password?"],
        "auth_state": "sign_in_gate",
    }

    with mock.patch.object(mod, "_ensure_workday_application_context", return_value=False):
        with mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            side_effect=[
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                rejected_markers,
                rejected_markers,
            ],
        ):
            with mock.patch.object(mod, "_open_workday_sign_in"):
                with mock.patch.object(mod, "_open_workday_create_account"):
                    with mock.patch.object(mod, "_do_sign_in", return_value=False):
                        with mock.patch.object(mod, "_do_password_reset", return_value=False):
                            with mock.patch.object(mod, "_do_create_account", return_value=False):
                                result = mod._run_workday_auth_flow(
                                    page,
                                    "jerrisonli@gmail.com",
                                    "password",
                                    payload=payload,
                                    job_url=payload["job_url"],
                                    out_dir=None,
                                    enter_application_flow=False,
                                )

    assert result["ok"] is False
    assert result["result"]["status"] == "auth_failed"
    assert result["result"]["auth_state"] == "credential_rejected"
    assert result["result"]["last_attempted_step"] == "create_account"


def test_run_workday_auth_flow_recovers_resumable_application_context_even_after_false_step():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://autodesk.wd1.myworkdayjobs.com/Ext/job/example",
        "company": "Autodesk Inc.",
        "job_title": "Senior Principal Product Manager, Advanced Solutions",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": payload["job_url"],
        "page_text_excerpt": "Candidate Home Job Alerts View Application",
        "heading_text": "Senior Principal Product Manager, Advanced Solutions",
        "alert_text": "Senior Principal Product Manager, Advanced Solutions page is loaded",
        "visible_actions": ["Candidate Home", "View Application", "Search for Jobs"],
        "auth_state": "unknown",
    }

    with mock.patch.object(mod, "_ensure_workday_application_context", side_effect=[False, True]) as ensure_context:
        with mock.patch.object(mod, "_extract_workday_auth_markers", return_value=markers):
            with mock.patch.object(mod, "_open_workday_sign_in"):
                with mock.patch.object(mod, "_do_sign_in", return_value=False):
                    with mock.patch.object(mod, "_do_password_reset") as do_password_reset:
                        with mock.patch.object(mod, "_open_workday_create_account"):
                            with mock.patch.object(mod, "_do_create_account") as do_create_account:
                                result = mod._run_workday_auth_flow(
                                    page,
                                    "jerrisonli@gmail.com",
                                    "password",
                                    payload=payload,
                                    job_url=payload["job_url"],
                                    out_dir=None,
                                    enter_application_flow=False,
                                )

    assert result == {"ok": True}
    assert ensure_context.call_count == 2
    do_password_reset.assert_not_called()
    do_create_account.assert_not_called()


def test_run_workday_auth_flow_returns_already_applied_after_auth_recovers_job_page():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://autodesk.wd1.myworkdayjobs.com/Ext/job/example",
        "company": "Autodesk Inc.",
        "job_title": "Senior Principal Product Manager, Advanced Solutions",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": payload["job_url"],
        "page_text_excerpt": "You applied for this job on March 27, 2026. View Application",
        "heading_text": "Senior Principal Product Manager, Advanced Solutions",
        "alert_text": "",
        "visible_actions": ["Candidate Home", "View Application", "Search for Jobs"],
        "auth_state": "unknown",
    }

    with mock.patch.object(mod, "_ensure_workday_application_context", side_effect=[False, False]):
        with mock.patch.object(mod, "_extract_workday_auth_markers", return_value=markers):
            with mock.patch.object(mod, "_is_workday_already_applied_job_page", side_effect=[False, True]):
                with mock.patch.object(mod, "_open_workday_sign_in"):
                    with mock.patch.object(mod, "_do_sign_in", return_value=True):
                        with mock.patch.object(mod, "_do_password_reset") as do_password_reset:
                            with mock.patch.object(mod, "_open_workday_create_account"):
                                with mock.patch.object(mod, "_do_create_account") as do_create_account:
                                    result = mod._run_workday_auth_flow(
                                        page,
                                        "jerrisonli@gmail.com",
                                        "password",
                                        payload=payload,
                                        job_url=payload["job_url"],
                                        out_dir=None,
                                        enter_application_flow=False,
                                    )

    assert result == {"ok": True, "already_applied": True}
    do_password_reset.assert_not_called()
    do_create_account.assert_not_called()


def test_run_workday_auth_flow_continues_after_gws_auth_failure_and_records_it():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/example",
        "company": "FactSet",
        "job_title": "Senior Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    markers = {
        "page_url": "https://factset.wd108.myworkdayjobs.com/en-US/FactSetCareers/login",
        "page_text_excerpt": "Sign In Email Address Password Sign In Create Account Forgot your password?",
        "heading_text": "FactSet Careers",
        "alert_text": "",
        "visible_actions": ["Sign In", "Create Account", "Forgot your password?"],
        "auth_state": "sign_in_gate",
    }

    with mock.patch.object(mod, "_ensure_workday_application_context", return_value=False):
        with mock.patch.object(mod, "_extract_workday_auth_markers", return_value=markers):
            with mock.patch.object(mod, "_open_workday_sign_in"):
                with mock.patch.object(mod, "_open_workday_create_account"):
                    with mock.patch.object(mod, "_do_sign_in", return_value=False):
                        with mock.patch.object(
                            mod,
                            "_do_password_reset",
                            side_effect=RuntimeError("invalid_grant: Token has been expired or revoked."),
                        ):
                            with mock.patch.object(mod, "_do_create_account", return_value=False) as do_create_account:
                                result = mod._run_workday_auth_flow(
                                    page,
                                    "jerrisonli@gmail.com",
                                    "password",
                                    payload=payload,
                                    job_url=payload["job_url"],
                                    out_dir=None,
                                    enter_application_flow=False,
                                )

    assert result["ok"] is False
    assert result["result"]["last_attempted_step"] == "create_account"
    assert "invalid_grant" in result["result"]["alert_text"]
    do_create_account.assert_called_once()


def test_run_workday_auth_flow_preserves_last_informative_auth_state_on_error_page():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = object()
    payload = {
        "job_url": "https://liveramp.wd5.myworkdayjobs.com/LiveRampCareers/job/example",
        "company": "LiveRamp",
        "job_title": "Lead Product Manager",
        "candidate_email": "jerrisonli@gmail.com",
    }
    sign_in_markers = {
        "page_url": "https://liveramp.wd5.myworkdayjobs.com/en-US/LiveRampCareers/login",
        "page_text_excerpt": "Sign In Create Account Forgot your password?",
        "heading_text": "Sign In",
        "alert_text": "",
        "visible_actions": ["Sign In", "Create Account", "Forgot your password?"],
        "auth_state": "sign_in_gate",
    }
    verification_markers = {
        "page_url": "https://liveramp.wd5.myworkdayjobs.com/en-US/LiveRampCareers/login",
        "page_text_excerpt": "Verify your account before you sign in.",
        "heading_text": "Sign In",
        "alert_text": "Verify your account before you sign in or request a verification email.",
        "visible_actions": ["Resend Account Verification", "Sign In"],
        "auth_state": "account_verification_gate",
    }
    error_markers = {
        "page_url": "chrome-error://chromewebdata/",
        "page_text_excerpt": "This page isn't working. HTTP ERROR 406.",
        "heading_text": "This page isn't working",
        "alert_text": "Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://liveramp.wd5.myworkdayjobs.com/",
        "visible_actions": ["Reload"],
        "auth_state": "unknown",
    }

    with (
        mock.patch.object(mod, "_ensure_workday_application_context", return_value=False),
        mock.patch.object(mod, "_is_workday_already_applied_job_page", return_value=False),
        mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            side_effect=[
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                sign_in_markers,
                verification_markers,
                error_markers,
            ],
        ),
        mock.patch.object(mod, "_open_workday_sign_in"),
        mock.patch.object(mod, "_open_workday_create_account"),
        mock.patch.object(mod, "_do_sign_in", return_value=False),
        mock.patch.object(mod, "_do_password_reset", return_value=False),
        mock.patch.object(mod, "_do_create_account", return_value=False),
    ):
        result = mod._run_workday_auth_flow(
            page,
            "jerrisonli@gmail.com",
            "password",
            payload=payload,
            job_url=payload["job_url"],
            out_dir=None,
            enter_application_flow=False,
        )

    assert result["ok"] is False
    assert result["result"]["last_attempted_step"] == "create_account"
    assert result["result"]["auth_state"] == "account_verification_gate"
    assert "HTTP_RESPONSE_CODE_FAILURE" in result["result"]["alert_text"]


def test_write_workday_failed_result_persists_my_information_validation_failure(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    out_dir = tmp_path / "job-output"
    payload = {
        "job_url": "https://autodesk.wd1.myworkdayjobs.com/Ext/job/example",
        "company": "Autodesk Inc.",
        "job_title": "Senior Principal Product Manager, Advanced Solutions",
        "artifacts": {
            "submit_debug_screenshot": str(out_dir / "submit" / "workday_submit_debug.png"),
        },
    }
    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
    debug_png.parent.mkdir(parents=True, exist_ok=True)
    debug_png.write_text("png", encoding="utf-8")

    mod._write_workday_failed_result(
        out_dir,
        payload,
        failure_type="my_information_validation",
        message="Workday My Information page still shows required validation errors.",
        current_page="my_information",
        validation_errors=["How Did You Hear About Us?"],
    )

    result_path = out_dir / "submit" / "application_submission_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["status"] == "failed"
    assert result["board"] == "workday"
    assert result["failure_type"] == "my_information_validation"
    assert result["current_page"] == "my_information"
    assert result["validation_errors"] == ["How Did You Hear About Us?"]
    assert result["artifacts"]["submit_debug_screenshot"] == str(debug_png)


def test_workday_validation_failure_for_page_uses_page_specific_failure_type():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    failure_type, message = mod._workday_validation_failure_for_page("my_experience")

    assert failure_type == "my_experience_validation"
    assert message == "Workday My Experience page still shows required validation errors after repeated retry attempts."


def test_is_workday_apply_url_accepts_apply_root_and_nested_apply_paths():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert mod._is_workday_apply_url("https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/foo/apply")
    assert mod._is_workday_apply_url("https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/foo/apply/applyManually")
    assert mod._is_workday_apply_url("https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/foo/apply?step=1")
    assert not mod._is_workday_apply_url("https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/foo")


def test_is_application_page_accepts_public_introduce_yourself_form():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    first_name = _ActionItem()
    resume_upload = _ActionItem()
    submit_button = _ActionItem()
    page = _ActionPage(
        locators={
            "input[type='file']": _ActionLocator.single(resume_upload),
        },
        role_locators={
            ("textbox", "First Name"): _ActionLocator.single(first_name),
            ("button", "Submit"): _ActionLocator.single(submit_button),
        },
        url="https://outsystems.wd503.myworkdayjobs.com/en-US/OutSystems/introduceYourself",
    )

    assert mod._is_application_page(page) is True


def test_is_application_page_accepts_public_question_page_with_sign_in_header():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    next_button = _ActionItem()
    sign_in_button = _ActionItem()
    page = _ActionPage(
        role_locators={
            ("button", "Next"): _ActionLocator.single(next_button),
            ("button", "Sign In"): _ActionLocator.single(sign_in_button),
        },
        url="https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/example/apply/applyManually",
    )

    assert mod._is_application_page(page) is True


def test_is_application_page_rejects_sign_in_shell_with_apply_url_and_submit_control():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    sign_in_shell = _ActionItem()
    submit_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='signInContent']": _ActionLocator.single(sign_in_shell),
        },
        role_locators={
            ("button", "Submit"): _ActionLocator.single(submit_button),
        },
        url="https://servicetitan.wd1.myworkdayjobs.com/en-US/ServiceTitan/job/example/apply/applyManually",
    )

    assert mod._is_application_page(page) is False


def test_run_workday_auth_flow_short_circuits_public_introduce_yourself_form():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    first_name = _ActionItem()
    resume_upload = _ActionItem()
    submit_button = _ActionItem()
    page = _ActionPage(
        locators={
            "input[type='file']": _ActionLocator.single(resume_upload),
        },
        role_locators={
            ("textbox", "First Name"): _ActionLocator.single(first_name),
            ("button", "Submit"): _ActionLocator.single(submit_button),
        },
        url="https://outsystems.wd503.myworkdayjobs.com/en-US/OutSystems/introduceYourself",
    )
    payload = {
        "job_url": "https://outsystems.wd503.myworkdayjobs.com/OutSystems/job/example",
        "company": "OutSystems, Inc.",
        "job_title": "Outbound Product Management Director - Public Sector",
        "candidate_email": "jerrisonli@gmail.com",
    }

    result = mod._run_workday_auth_flow(
        page,
        "jerrisonli@gmail.com",
        "password",
        payload=payload,
        job_url=payload["job_url"],
        out_dir=None,
        enter_application_flow=False,
    )

    assert result == {"ok": True}


def test_parse_workday_employment_entries_parses_current_role_from_master_resume_format():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    entries = mod._parse_workday_employment_entries(
        [
            "EXPERIENCE",
            "MOODY'S ANALYTICS — Associate Director, Product Management",
            "San Francisco, CA | August 2024–Present",
            "* Bullet",
            "EDUCATION",
        ]
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.company == "MOODY'S ANALYTICS"
    assert entry.title == "Associate Director, Product Management"
    assert entry.location == "San Francisco, CA"
    assert entry.start_month == "8"
    assert entry.start_year == "2024"
    assert entry.is_current is True


def test_parse_workday_employment_entries_parses_completed_role_end_date():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    entries = mod._parse_workday_employment_entries(
        [
            "EXPERIENCE",
            "KYTE — Staff Product Manager",
            "San Francisco, CA | Mar 2022–August 2024 | 150 employees",
            "* Bullet",
            "EDUCATION",
        ]
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.start_month == "3"
    assert entry.start_year == "2022"
    assert entry.end_month == "8"
    assert entry.end_year == "2024"
    assert entry.is_current is False


def test_deterministic_workday_question_value_uses_yes_no_for_select_work_authorization():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Are you eligible to work in the country in which this position is located?",
        "select",
        application_profile,
    )

    assert value == "Yes"


def test_deterministic_workday_question_value_keeps_statement_for_text_work_authorization():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Please describe your work authorization status",
        "text",
        application_profile,
    )

    assert value == application_profile.work_authorization_statement


def test_deterministic_workday_question_value_uses_sponsorship_answer_for_text_employment_based_status():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        (
            "Will you now or in the future require our company to file a petition or application "
            "for employment-based immigration status on your behalf to begin or continue employment "
            "with our company?"
        ),
        "text",
        application_profile,
    )

    assert value == "No"


def test_deterministic_workday_question_value_uses_no_for_select_sponsorship():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Will you now or in the future require sponsorship for employment visa status?",
        "select",
        application_profile,
    )

    assert value == "No"


def test_deterministic_workday_question_value_uses_no_for_prior_employment_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Have you been employed by Adobe in the past?",
        "select",
        application_profile,
    )

    assert value == "No"


def test_deterministic_workday_question_value_uses_no_for_long_sponsorship_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status, spouse visa, etc)?",
        "select",
        application_profile,
    )

    assert value == "No"


def test_deterministic_workday_question_value_uses_no_for_visa_support_extension_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Will you need visa support to continue or extend your current work authorization status? (ex.. H1-B, TN, OPT, CPT, etc.)",
        "select",
        application_profile,
    )

    assert value == "No"


def test_deterministic_workday_question_value_uses_yes_for_legal_age_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Are you of legal age to work in the country in which this position will be based?",
        "select",
        application_profile,
    )

    assert value == "Yes"


def test_deterministic_workday_question_value_returns_pronoun_option_for_select_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    options = ["She/Her", "He/Him", "They/Them"]

    value = mod._deterministic_workday_question_value(
        "(Optional) Please share your preferred pronouns:",
        "select",
        application_profile,
        options,
    )

    assert value == "He/Him"


def test_deterministic_workday_question_value_uses_yes_for_background_check_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Are you willing to submit a background check during the hiring process?",
        "radio",
        application_profile,
    )

    assert value == "Yes"


def test_deterministic_workday_question_value_uses_yes_for_background_check_undergo_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "If selected for this job, will you be willing to undergo Centific's background check process?",
        "select",
        application_profile,
    )

    assert value == "Yes"


def test_deterministic_workday_question_value_uses_profile_travel_percentage_for_open_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "Please mention how much % can travel?",
        "text",
        application_profile,
    )

    assert value == "50%"


def test_deterministic_workday_question_value_uses_profile_state_for_remote_us_location_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    value = mod._deterministic_workday_question_value(
        "If applying to Remote US location, what state(s) are you able to work in?",
        "textarea",
        application_profile,
    )

    assert value == "California"


def test_workday_bare_date_prompt_uses_current_date():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    value = mod._workday_bare_date_answer("1.DATE:", now=datetime(2026, 4, 5, 12, 0, tzinfo=UTC))

    assert value == "04/05/2026"


def test_deterministic_workday_question_value_handles_follow_up_work_authorization_status_select():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    options = [
        "I am authorized to work for any employer in the country where this job is located",
        "I require sponsorship to work in the country where this job is located",
    ]

    value = mod._deterministic_workday_question_value(
        "You answered 'Yes' to the previous question. Please choose the answer below which most accurately fits your situation.",
        "select",
        application_profile,
        options,
    )

    assert value == options[0]


def test_deterministic_workday_question_value_handles_permanent_work_authorization_follow_up():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    options = [
        "I am authorized to work permanently in the country",
        "I have an active temporary work permit",
    ]

    value = mod._deterministic_workday_question_value(
        "You answered 'Yes' to the previous question. Please choose the answer below which most accurately fits your situation.",
        "select",
        application_profile,
        options,
    )

    assert value == options[0]


def test_deterministic_workday_question_value_handles_citizen_follow_up_when_resume_supports_it():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    options = [
        "I am a citizen of the country where this job is located",
        "I require sponsorship to work in the country where this job is located",
    ]

    value = mod._deterministic_workday_question_value(
        "You answered 'Yes' to the previous question. Please choose the answer below which most accurately fits your situation.",
        "select",
        application_profile,
        options,
    )

    assert value == options[0]


def test_fill_application_questions_uses_live_dropdown_options_for_deterministic_select(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return (
                "You answered 'Yes' to the previous question. Please choose the answer below which most accurately "
                "fits your situation."
            )

    class _QuestionDropdown:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

    class _QuestionGroup:
        def __init__(self) -> None:
            self.label = _QuestionLabel()
            self.dropdown = _QuestionDropdown()

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector == "textarea":
                return _EmptyLocator()
            if selector == "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']":
                return self.dropdown
            if selector == "input[type='radio']":
                return _EmptyLocator()
            if selector == "input[type='checkbox']":
                return _EmptyLocator()
            if selector == "input[type='text']":
                return _EmptyLocator()
            return _EmptyLocator()

    group = _QuestionGroup()
    page = mock.Mock()
    page.locator.side_effect = lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    page.wait_for_timeout.return_value = None

    options = [
        "I am authorized to work permanently in the country",
        "I have an active temporary work permit",
    ]

    with (
        mock.patch.object(mod, "_workday_dropdown_option_texts", return_value=options),
        mock.patch.object(mod, "_fill_workday_dropdown_locator", return_value=True) as fill_dropdown,
    ):
        filled = mod._fill_application_questions(page, tmp_path, {}, provider=None)

    assert fill_dropdown.call_args.args[2] == options[0]
    assert any(entry["label"].startswith("You answered 'Yes'") for entry in filled)


def test_fill_application_questions_prefers_visible_text_input_for_deterministic_text(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "Salary Expectations"

    class _QuestionGroup:
        def __init__(self, hidden_input, visible_input) -> None:
            self.label = _QuestionLabel()
            self.text_inputs = _LocatorCollection([hidden_input, visible_input])

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector in ("input[type='text']", "input[type='text'], input:not([type])"):
                return self.text_inputs
            if selector == "textarea":
                return _EmptyLocator()
            if selector == "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']":
                return _EmptyLocator()
            if selector == "input[type='radio']":
                return _EmptyLocator()
            if selector == "input[type='checkbox']":
                return _EmptyLocator()
            return _EmptyLocator()

    hidden_input = _VisibilityFieldLocator(visible=False)
    visible_input = _VisibilityFieldLocator(visible=True)
    group = _QuestionGroup(hidden_input, visible_input)
    page = mock.Mock()
    page.locator.side_effect = (
        lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    )
    page.wait_for_timeout.return_value = None

    with mock.patch.object(
        mod,
        "_deterministic_workday_question_value",
        return_value="I'm open and flexible on compensation.",
    ):
        filled = mod._fill_application_questions(page, tmp_path, {}, provider=None)

    assert hidden_input.fill_calls == []
    assert visible_input.fill_calls == ["I'm open and flexible on compensation."]
    assert any(entry["label"] == "Salary Expectations" for entry in filled)


def test_fill_application_questions_prefers_visible_text_input_for_generated_text(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "Tell us about a time you influenced a product roadmap."

    class _QuestionGroup:
        def __init__(self, hidden_input, visible_input) -> None:
            self.label = _QuestionLabel()
            self.text_inputs = _LocatorCollection([hidden_input, visible_input])

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector in ("input[type='text']", "input[type='text'], input:not([type])"):
                return self.text_inputs
            if selector == "textarea":
                return _EmptyLocator()
            if selector == "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']":
                return _EmptyLocator()
            if selector == "input[type='radio']":
                return _EmptyLocator()
            if selector == "input[type='checkbox']":
                return _EmptyLocator()
            return _EmptyLocator()

    hidden_input = _VisibilityFieldLocator(visible=False)
    visible_input = _VisibilityFieldLocator(visible=True)
    group = _QuestionGroup(hidden_input, visible_input)
    page = mock.Mock()
    page.locator.side_effect = (
        lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    )
    page.wait_for_timeout.return_value = None

    with (
        mock.patch.object(mod, "_deterministic_workday_question_value", return_value=None),
        mock.patch.object(
            mod,
            "generate_application_answers",
            return_value={"tell_us_about_a_time_you_influenced_a_product_roadmap": "Generated answer"},
        ),
    ):
        filled = mod._fill_application_questions(page, tmp_path, {}, provider=None)

    assert hidden_input.fill_calls == []
    assert visible_input.fill_calls == ["Generated answer"]
    assert any(entry["value"] == "Generated answer" for entry in filled)


def test_fill_application_questions_fills_segmented_date_prompt(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "1.DATE:"

    class _DateSegmentLocator(_VisibilityFieldLocator):
        def __init__(self) -> None:
            super().__init__(visible=True)
            self.type_calls: list[str] = []
            self.press_calls: list[str] = []
            self.evaluate_calls: list[str] = []

        def scroll_into_view_if_needed(self) -> None:
            return None

        def click(self, force: bool = False, timeout: int | None = None) -> None:
            del force, timeout
            self.click_calls += 1

        def focus(self) -> None:
            return None

        def press(self, key: str) -> None:
            self.press_calls.append(key)
            if key == "Backspace":
                self.value = ""

        def fill(self, value: str) -> None:
            self.fill_calls.append(value)
            self.value = value

        def type(self, value: str, delay: int = 0) -> None:
            del delay
            self.type_calls.append(value)
            self.value = value

        def evaluate(self, script: str, value: str) -> str:
            del script
            self.evaluate_calls.append(value)
            self.value = value
            return value

    class _QuestionGroup:
        def __init__(self, month, day, year) -> None:
            self.label = _QuestionLabel()
            self.date_inputs = _LocatorCollection([month, day, year])
            self.month = _LocatorCollection([month])
            self.day = _LocatorCollection([day])
            self.year = _LocatorCollection([year])

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector == (
                "[data-automation-id='dateInputWrapper'], "
                "[data-automation-id='dateSectionMonth-input'], "
                "[data-automation-id='dateSectionDay-input'], "
                "[data-automation-id='dateSectionYear-input']"
            ):
                return self.date_inputs
            if selector == "[data-automation-id='dateSectionMonth-input'], input[aria-label='Month']":
                return self.month
            if selector == "[data-automation-id='dateSectionDay-input'], input[aria-label='Day']":
                return self.day
            if selector == "[data-automation-id='dateSectionYear-input'], input[aria-label='Year']":
                return self.year
            if selector in (
                "textarea",
                "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']",
                "input[type='radio']",
                "input[type='checkbox']",
                "input[type='text'], input:not([type])",
            ):
                return _EmptyLocator()
            return _EmptyLocator()

    month = _DateSegmentLocator()
    day = _DateSegmentLocator()
    year = _DateSegmentLocator()
    group = _QuestionGroup(month, day, year)
    page = mock.Mock()
    page.locator.side_effect = (
        lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    )
    page.wait_for_timeout.return_value = None

    with (
        mock.patch.object(mod, "_deterministic_workday_question_value", return_value="04/05/2026"),
        mock.patch.object(mod, "generate_application_answers", return_value={}) as generate_answers,
    ):
        filled = mod._fill_application_questions(page, tmp_path, {}, provider=None)

    generate_answers.assert_not_called()
    assert month.type_calls == ["4"]
    assert day.type_calls == ["5"]
    assert year.type_calls == ["2026"]
    assert any(entry["label"] == "1.DATE:" for entry in filled)


def test_fill_workday_date_segment_retries_with_dom_setter_when_typed_value_does_not_stick():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _DateSegmentLocator:
        def __init__(self) -> None:
            self.value = ""
            self.fill_calls: list[str] = []
            self.type_calls: list[str] = []
            self.press_calls: list[str] = []
            self.evaluate_calls: list[str] = []

        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self) -> None:
            pass

        def click(self, force: bool = False, timeout: int | None = None) -> None:
            del force, timeout

        def focus(self) -> None:
            pass

        def press(self, key: str) -> None:
            self.press_calls.append(key)
            if key == "Backspace":
                self.value = ""

        def fill(self, value: str) -> None:
            self.fill_calls.append(value)
            self.value = ""

        def type(self, value: str, delay: int = 0) -> None:
            del delay
            self.type_calls.append(value)
            self.value = ""

        def evaluate(self, script: str, value: str) -> str:
            del script
            self.evaluate_calls.append(value)
            self.value = value
            return value

        def input_value(self) -> str:
            return self.value

    locator = _DateSegmentLocator()

    assert mod._fill_workday_date_segment(locator, "5")
    assert locator.evaluate_calls == ["5"]
    assert locator.input_value() == "5"


def test_fill_workday_date_segment_tabs_after_typed_value_sticks_without_dom_setter():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _DateSegmentLocator:
        def __init__(self) -> None:
            self.value = ""
            self.fill_calls: list[str] = []
            self.type_calls: list[str] = []
            self.press_calls: list[str] = []
            self.evaluate_calls: list[str] = []

        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self) -> None:
            pass

        def click(self, force: bool = False, timeout: int | None = None) -> None:
            del force, timeout

        def focus(self) -> None:
            pass

        def press(self, key: str) -> None:
            self.press_calls.append(key)
            if key == "Backspace":
                self.value = ""

        def fill(self, value: str) -> None:
            self.fill_calls.append(value)
            self.value = value

        def type(self, value: str, delay: int = 0) -> None:
            del delay
            self.type_calls.append(value)
            self.value = value

        def evaluate(self, script: str, value: str) -> str:
            del script
            self.evaluate_calls.append(value)
            self.value = value
            return value

        def input_value(self) -> str:
            return self.value

    locator = _DateSegmentLocator()

    assert mod._fill_workday_date_segment(locator, "5")
    assert locator.type_calls == ["5"]
    assert locator.evaluate_calls == []
    assert locator.press_calls[-1] == "Tab"
    assert locator.input_value() == "5"


def test_fill_workday_segmented_date_scope_uses_keyboard_section_navigation():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _SegmentLocator(_VisibilityFieldLocator):
        def __init__(self, segment: str, shared_state: dict[str, object]) -> None:
            super().__init__(value="")
            self.segment = segment
            self.shared_state = shared_state
            self.focus_calls = 0
            self.type_calls: list[str] = []
            self.press_calls: list[str] = []

        def focus(self) -> None:
            self.focus_calls += 1
            self.shared_state["active"] = self.segment

        def click(self, force: bool = False, timeout: int | None = None) -> None:
            del force, timeout
            raise AssertionError("keyboard-section navigation should not rely on click focus")

        def type(self, value: str, delay: int = 0) -> None:
            del delay
            self.type_calls.append(value)
            self.shared_state[self.segment] = value

        def press_sequentially(self, value: str, delay: int = 0) -> None:
            self.type(value, delay)

        def press(self, key: str) -> None:
            self.press_calls.append(key)
            if key == "ArrowRight":
                if self.segment == "month":
                    self.shared_state["active"] = "day"
                elif self.segment == "day":
                    self.shared_state["active"] = "year"
            elif key == "Tab":
                self.shared_state["blurred"] = True

        def input_value(self) -> str:
            return str(self.shared_state.get(self.segment, ""))

    class _QuestionGroup:
        def __init__(self, month, day, year) -> None:
            self.month = _LocatorCollection([month])
            self.day = _LocatorCollection([day])
            self.year = _LocatorCollection([year])

        def locator(self, selector: str):
            if selector == "[data-automation-id='dateSectionMonth-input'], input[aria-label='Month']":
                return self.month
            if selector == "[data-automation-id='dateSectionDay-input'], input[aria-label='Day']":
                return self.day
            if selector == "[data-automation-id='dateSectionYear-input'], input[aria-label='Year']":
                return self.year
            if selector == "p[data-automation-id='inputAlert'], [data-automation-id='inputAlert']":
                return _EmptyLocator()
            return _EmptyLocator()

    shared_state: dict[str, object] = {"month": "", "day": "", "year": "", "active": None}
    month = _SegmentLocator("month", shared_state)
    day = _SegmentLocator("day", shared_state)
    year = _SegmentLocator("year", shared_state)
    group = _QuestionGroup(month, day, year)

    assert mod._fill_workday_segmented_date_scope(group, "04/05/2026")
    assert month.focus_calls == 1
    assert day.focus_calls == 1
    assert year.focus_calls == 1
    assert month.type_calls == ["4"]
    assert day.type_calls == ["5"]
    assert year.type_calls == ["2026"]
    assert month.press_calls == ["ArrowRight"]
    assert day.press_calls == ["ArrowRight"]
    assert year.press_calls == ["Tab"]
    assert shared_state == {
        "month": "4",
        "day": "5",
        "year": "2026",
        "active": "year",
        "blurred": True,
    }


def test_fill_workday_self_identify_date_scope_types_contiguous_digits_from_month():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _Keyboard:
        def __init__(self, shared_state: dict[str, object]) -> None:
            self.shared_state = shared_state
            self.type_calls: list[str] = []
            self.press_calls: list[str] = []

        def type(self, value: str, delay: int = 0) -> None:
            del delay
            self.type_calls.append(value)
            self.shared_state["typed_date"] = value

        def press(self, key: str) -> None:
            self.press_calls.append(key)
            if key == "Tab":
                self.shared_state["blurred"] = True
                if self.shared_state.get("typed_date") == "04052026":
                    self.shared_state["month"] = "4"
                    self.shared_state["day"] = "5"
                    self.shared_state["year"] = "2026"

    class _SegmentLocator(_VisibilityFieldLocator):
        def __init__(self, segment: str, shared_state: dict[str, object]) -> None:
            super().__init__(value="")
            self.segment = segment
            self.shared_state = shared_state
            self.focus_calls = 0

        def focus(self) -> None:
            self.focus_calls += 1
            self.shared_state["active"] = self.segment

        def click(self, force: bool = False, timeout: int | None = None) -> None:
            del force, timeout
            raise AssertionError("self-identify date should not rely on click focus")

        def input_value(self) -> str:
            return str(self.shared_state.get(self.segment, ""))

    class _QuestionGroup:
        def __init__(self, month, day, year) -> None:
            self.month = _LocatorCollection([month])
            self.day = _LocatorCollection([day])
            self.year = _LocatorCollection([year])

        def locator(self, selector: str):
            if selector == "[data-automation-id='dateSectionMonth-input'], input[aria-label='Month']":
                return self.month
            if selector == "[data-automation-id='dateSectionDay-input'], input[aria-label='Day']":
                return self.day
            if selector == "[data-automation-id='dateSectionYear-input'], input[aria-label='Year']":
                return self.year
            return _EmptyLocator()

    shared_state: dict[str, object] = {"month": "", "day": "", "year": "", "active": None}
    page = SimpleNamespace(
        keyboard=_Keyboard(shared_state),
        wait_for_timeout=lambda _timeout_ms: None,
    )
    month = _SegmentLocator("month", shared_state)
    day = _SegmentLocator("day", shared_state)
    year = _SegmentLocator("year", shared_state)
    group = _QuestionGroup(month, day, year)

    assert mod._fill_workday_self_identify_date_scope(page, group, "04/05/2026")
    assert month.focus_calls == 1
    assert day.focus_calls == 0
    assert year.focus_calls == 0
    assert page.keyboard.type_calls == ["04052026"]
    assert page.keyboard.press_calls == ["Tab"]
    assert shared_state == {
        "month": "4",
        "day": "5",
        "year": "2026",
        "active": "month",
        "blurred": True,
        "typed_date": "04052026",
    }


def test_fill_workday_question_date_refills_invalid_group_even_when_values_match():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _InputAlert:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "Error: Enter today's date"

    class _DateSegmentLocator(_VisibilityFieldLocator):
        pass

    class _QuestionGroup:
        def __init__(self, month, day, year) -> None:
            self.date_inputs = _LocatorCollection([month, day, year])
            self.month = _LocatorCollection([month])
            self.day = _LocatorCollection([day])
            self.year = _LocatorCollection([year])
            self.alert = _LocatorCollection([_InputAlert()])

        def locator(self, selector: str):
            if selector == (
                "[data-automation-id='dateInputWrapper'], "
                "[data-automation-id='dateSectionMonth-input'], "
                "[data-automation-id='dateSectionDay-input'], "
                "[data-automation-id='dateSectionYear-input']"
            ):
                return self.date_inputs
            if selector == "[data-automation-id='dateSectionMonth-input'], input[aria-label='Month']":
                return self.month
            if selector == "[data-automation-id='dateSectionDay-input'], input[aria-label='Day']":
                return self.day
            if selector == "[data-automation-id='dateSectionYear-input'], input[aria-label='Year']":
                return self.year
            if selector == "p[data-automation-id='inputAlert'], [data-automation-id='inputAlert']":
                return self.alert
            return _EmptyLocator()

    month = _DateSegmentLocator(value="4")
    day = _DateSegmentLocator(value="5")
    year = _DateSegmentLocator(value="2026")
    group = _QuestionGroup(month, day, year)

    with mock.patch.object(
        mod,
        "_fill_workday_segmented_date_scope_with_navigation",
        return_value=True,
    ) as fill_segment:
        assert mod._fill_workday_question_date(group, "04/05/2026")

    fill_segment.assert_called_once_with(group, "4", "5", "2026")


def test_visible_workday_validation_errors_captures_bare_enter_messages():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _ValidationNode:
        def __init__(self, text: str, *, described_by: str = "") -> None:
            self.text = text
            self.described_by = described_by

        def inner_text(self) -> str:
            return self.text

        def get_attribute(self, name: str) -> str:
            if name == "aria-describedby":
                return self.described_by
            return ""

    class _DescribedNode:
        def __init__(self, text: str) -> None:
            self.text = text

        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return self.text

    page = mock.Mock()
    page.locator.side_effect = lambda selector: (
        _LocatorCollection([_ValidationNode("Error: Enter today's date")])
        if selector
        == (
            "[role='alert'], .error, .errorMessage, .css-14d18rb, "
            "[data-automation-id='errorHeading'] button, "
            "p[data-automation-id='inputAlert'], "
            "[id^='hint']"
        )
        else _DescribedNode("")
    )
    page.inner_text.return_value = ""

    assert mod._visible_workday_validation_errors(page) == ["Enter today's date"]


def test_fill_voluntary_disclosures_selects_disability_before_date_commit():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = SimpleNamespace(
        race_or_ethnicity="",
        gender="",
        veteran_status="",
        disability_status="No, I do not have a disability and have not had one in the past",
    )
    page = mock.Mock()
    date_scope = _FieldLocator()
    page.locator.side_effect = lambda selector: (
        date_scope
        if selector == "[data-fkit-id$='--dateSignedOn'], [data-automation-id='formField-dateSignedOn']"
        else _EmptyLocator()
    )
    call_order: list[str] = []

    with (
        mock.patch.object(mod, "_fill_workday_dropdown", return_value=False),
        mock.patch.object(mod, "_fill_workday_labeled_dropdown_candidates", return_value=False),
        mock.patch.object(mod, "_fill_workday_labeled_dropdown", return_value=False),
        mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=False),
        mock.patch.object(mod, "_try_radio_or_checkbox", return_value=None),
        mock.patch.object(
            mod,
            "_select_workday_checkbox_option",
            side_effect=lambda *_args, **_kwargs: call_order.append("checkbox") or True,
        ),
        mock.patch.object(
            mod,
            "_fill_workday_self_identify_date_scope",
            side_effect=lambda *_args, **_kwargs: call_order.append("date") or True,
        ),
    ):
        mod._fill_voluntary_disclosures(page, application_profile, profile=None)

    assert call_order == ["checkbox", "date"]


def test_fill_application_questions_uses_numeric_compensation_fallback_when_text_is_rejected(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "Salary Expectations"

    class _NumericOnlyField(_VisibilityFieldLocator):
        def fill(self, value: str) -> None:
            self.fill_calls.append(value)
            self.value = value if value.isdigit() else ""

    class _QuestionGroup:
        def __init__(self, text_input) -> None:
            self.label = _QuestionLabel()
            self.text_inputs = _LocatorCollection([text_input])

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector in ("input[type='text']", "input[type='text'], input:not([type])"):
                return self.text_inputs
            if selector in (
                "textarea",
                "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']",
                "input[type='radio']",
                "input[type='checkbox']",
                "[data-automation-id='dateInputWrapper'], [data-automation-id='dateSectionMonth-input'], "
                "[data-automation-id='dateSectionDay-input'], [data-automation-id='dateSectionYear-input']",
            ):
                return _EmptyLocator()
            return _EmptyLocator()

    numeric_only_input = _NumericOnlyField(visible=True)
    group = _QuestionGroup(numeric_only_input)
    page = mock.Mock()
    page.locator.side_effect = (
        lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    )
    page.wait_for_timeout.return_value = None

    with (
        mock.patch.object(mod, "parse_application_profile", return_value=application_profile),
        mock.patch.object(mod, "_deterministic_workday_question_value", return_value="I'm open and flexible on compensation."),
        mock.patch.object(mod, "generate_application_answers", return_value={}) as generate_answers,
    ):
        filled = mod._fill_application_questions(page, tmp_path, {}, provider=None)

    generate_answers.assert_not_called()
    assert numeric_only_input.fill_calls[0] == "I'm open and flexible on compensation."
    assert "1000" in numeric_only_input.fill_calls
    assert numeric_only_input.input_value() == "1000"
    assert any(entry["label"] == "Salary Expectations" and entry["value"] == "1000" for entry in filled)


def test_fill_application_questions_raises_pending_user_input_for_numeric_only_compensation_select(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _QuestionLabel:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

        def inner_text(self) -> str:
            return "My realistic base gross annual salary expectation (not including bonus/commission) for my next role is:"

    class _QuestionDropdown:
        def count(self) -> int:
            return 1

        @property
        def first(self):
            return self

    class _QuestionGroup:
        def __init__(self) -> None:
            self.label = _QuestionLabel()
            self.dropdown = _QuestionDropdown()

        def locator(self, selector: str):
            if selector == (
                "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, "
                "[id^='rich-label']"
            ):
                return self.label
            if selector == "textarea":
                return _EmptyLocator()
            if selector == "select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']":
                return self.dropdown
            if selector == "input[type='radio']":
                return _EmptyLocator()
            if selector == "input[type='checkbox']":
                return _EmptyLocator()
            if selector == "input[type='text']":
                return _EmptyLocator()
            return _EmptyLocator()

    group = _QuestionGroup()
    page = mock.Mock()
    page.locator.side_effect = (
        lambda selector: _LocatorCollection([group]) if selector == "[data-automation-id^='formField-']" else _EmptyLocator()
    )
    page.wait_for_timeout.return_value = None

    options = [
        "Select One",
        "$40,000 - $60,000",
        "$60,000 - $80,000",
        "$80,000 - $100,000",
    ]

    with (
        mock.patch.object(mod, "_workday_dropdown_option_texts", return_value=options),
        mock.patch.object(mod, "_fill_workday_dropdown_locator", return_value=False),
        mock.patch.object(mod, "generate_application_answers", return_value={}) as generate_answers,
        pytest.raises(mod.GeneratedAnswerBlockersError) as excinfo,
    ):
        mod._fill_application_questions(page, tmp_path, {}, provider=None)

    generate_answers.assert_not_called()
    blocker = excinfo.value.blockers[0]
    assert blocker["field_name"] == (
        "my_realistic_base_gross_annual_salary_expectation_not_including_bonus_commission_for_my_next_role_is"
    )
    assert blocker["blocker_kind"] == "generated_answer"
    assert "numeric salary ranges" in blocker["reason"]


def test_workday_checkbox_answer_values_select_negative_prior_employment_option():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )

    answers = mod._workday_checkbox_answer_values(
        "Have you ever worked at Adobe in the following capacity:",
        [
            "Employee",
            "Intern",
            "Temporary Agency or Vendor",
            "Other",
            "I have not worked for Adobe in the past.",
        ],
        application_profile,
    )

    assert answers == ["I have not worked for Adobe in the past."]


def test_fill_application_questions_extracts_checkbox_options_from_page_wide_labels(tmp_path):
    mod = load_module("autofill_workday_checkbox_options", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )
    out_dir = tmp_path / "job-output"
    (out_dir / "submit").mkdir(parents=True)
    html = """
    <div data-automation-id="formField-affiliations">
      <fieldset>
        <legend><label>Please select any Afflilations.</label></legend>
        <div role="grid">
          <div role="row">
            <div role="cell"><input id="affiliation-latinas" type="checkbox" /></div>
          </div>
          <div role="row">
            <div role="cell"><input id="affiliation-wharton" type="checkbox" /></div>
          </div>
        </div>
      </fieldset>
    </div>
    <div id="portaled-option-labels">
      <label for="affiliation-latinas">Latinas in Tech</label>
      <label for="affiliation-wharton">Wharton Alumni Familia</label>
    </div>
    """

    captured_specs: list[dict] = []

    def _fake_generate_application_answers(*, question_specs, **_kwargs):
        captured_specs.extend(question_specs)
        return {
            "please_select_any_afflilations": ["Wharton Alumni Familia"],
        }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        with (
            mock.patch.object(mod, "parse_application_profile", return_value=application_profile),
            mock.patch.object(mod, "generate_application_answers", side_effect=_fake_generate_application_answers),
        ):
            filled = mod._fill_application_questions(
                page,
                out_dir,
                {"company": "Autodesk", "board": "workday"},
                provider="openai",
            )

        assert captured_specs
        assert captured_specs[0]["field_name"] == "please_select_any_afflilations"
        assert captured_specs[0]["options"] == ["Latinas in Tech", "Wharton Alumni Familia"]
        assert page.locator('[id="affiliation-wharton"]').is_checked() is True
        assert any(
            entry["field_name"] == "please_select_any_afflilations"
            and entry["value"] == ["Wharton Alumni Familia"]
            for entry in filled
        )

        browser.close()


def test_persist_workday_question_answers_rewrites_stale_deterministic_checkbox_payload(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    answers_path = submit_dir / "application_answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-04-05T14:03:09+00:00",
                "provider": "deterministic_classification",
                "refresh_request_id": None,
                "questions": [],
                "answers": {
                    "have_you_ever_worked_at_adobe_in_the_following_capacity": ["No"],
                },
            }
        ),
        encoding="utf-8",
    )

    question_specs = [
        {
            "field_name": "have_you_ever_worked_at_adobe_in_the_following_capacity",
            "label": "Have you ever worked at Adobe in the following capacity:",
            "kind": "checkbox",
            "required": True,
            "options": [
                "Employee",
                "Intern",
                "Temporary Agency or Vendor",
                "Other",
                "I have not worked for Adobe in the past.",
            ],
            "type": "multi_value_multi_select",
        }
    ]

    mod._persist_workday_question_answers(
        out_dir,
        question_specs,
        {
            "have_you_ever_worked_at_adobe_in_the_following_capacity": [
                "I have not worked for Adobe in the past."
            ]
        },
        provider="openai",
    )

    payload = json.loads(answers_path.read_text(encoding="utf-8"))

    assert payload["provider"] == "deterministic_classification"
    assert payload["questions"] == question_specs
    assert payload["answers"] == {
        "have_you_ever_worked_at_adobe_in_the_following_capacity": [
            "I have not worked for Adobe in the past."
        ]
    }


def test_looks_like_workday_prior_employment_prompt_accepts_employed_in_the_past_variant():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert mod._looks_like_workday_prior_employment_prompt("Have you been employed by Adobe in the past?")


def test_write_workday_pending_user_input_for_generated_answer_blockers(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    payload = {
        "artifacts": {
            "report_json": str(submit_dir / "workday_autofill_report.json"),
            "report_markdown": str(submit_dir / "workday_autofill_report.md"),
            "pre_submit_screenshot": str(submit_dir / "workday_autofill_pre_submit.png"),
        }
    }
    blocker = {
        "field_name": "are_you_related_to_anyone_currently_employed_with_mcafee",
        "label": "Are you related to anyone currently employed with McAfee?",
        "kind": "radio",
        "required": True,
        "source": "generated_application_answer",
        "status": "planned",
        "blocker_kind": "generated_answer",
        "blocks_draft_completion": True,
        "reason": "The source bundle contains no information about relatives employed at McAfee.",
    }

    pending_path = mod._write_workday_pending_user_input_for_generated_answer_blockers(
        out_dir,
        payload,
        [blocker],
    )

    assert pending_path == submit_dir / "pending_user_input.json"
    pending_payload = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_payload["status"] == "pending_user_input"
    assert pending_payload["board"] == "workday"
    assert pending_payload["questions"][0]["field_name"] == blocker["field_name"]
    assert pending_payload["questions"][0]["blocker_kind"] == "generated_answer"


def test_workday_question_kind_prefers_checkbox_when_group_has_checkboxes():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert (
        mod._workday_question_kind(
            has_date=False,
            has_textarea=False,
            has_select=False,
            has_radio=False,
            has_checkbox=True,
        )
        == "checkbox"
    )


def test_workday_question_kind_prefers_date_when_group_has_date_inputs():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert (
        mod._workday_question_kind(
            has_date=True,
            has_textarea=False,
            has_select=False,
            has_radio=False,
            has_checkbox=False,
        )
        == "date"
    )


def test_workday_fill_if_visible_requires_retained_value_match():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _NonRetainingField(_VisibilityFieldLocator):
        def fill(self, value: str) -> None:
            self.fill_calls.append(value)
            self.value = ""

    locator = _NonRetainingField(visible=True)

    with mock.patch.object(mod, "human_fill", side_effect=lambda field, value, delay_ms=0: field.fill(value)):
        assert not mod._workday_fill_if_visible(locator, "Open and flexible")


def test_workday_checkbox_answer_values_prefers_existing_commute_option_for_hybrid_prompt():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    application_profile = mod.parse_application_profile(
        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
    )
    label = (
        "This is an in-office role, and it is subject to Turo's hybrid-work policy that currently requires "
        "in-office attendance three days a week on Mondays, Wednesdays, and Fridays. Do you live within "
        "commuting distance to the specified office location or are you open to relocating at your own expense?"
    )
    options = [
        "Yes, I already reside within commuting distance of this office location and understand the in-office component of this role.",
        "Yes, I am willing to relocate within commuting distance of this office location before starting my employment and understand the in-office component of this role.",
        "No, I do not reside within commuting distance of this office location or am not willing to relocate.",
    ]

    assert mod._workday_checkbox_answer_values(label, options, application_profile) == [options[0]]


def test_do_create_account_prefers_named_email_textbox_over_duplicate_data_email_locator():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    named_email = _ActionItem()
    duplicate_email_one = _ActionItem()
    duplicate_email_two = _ActionItem()
    password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator([duplicate_email_one, duplicate_email_two]),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(named_email),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert named_email.fill_calls == ["jerrisonli@gmail.com"]
    assert duplicate_email_one.fill_calls == []
    assert duplicate_email_two.fill_calls == []
    assert password.fill_calls == ["Secret123!"]
    assert verify_password.fill_calls == ["Secret123!"]
    assert create_button.click_calls == 1


def test_do_create_account_prefers_named_password_textbox_over_duplicate_data_password_locator():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    email = _ActionItem()
    named_password = _ActionItem()
    duplicate_password_one = _ActionItem()
    duplicate_password_two = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(email),
            "[data-automation-id='password']": _ActionLocator([duplicate_password_one, duplicate_password_two]),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(email),
            ("textbox", "Password"): _ActionLocator.single(named_password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert named_password.fill_calls == ["Secret123!"]
    assert duplicate_password_one.fill_calls == []
    assert duplicate_password_two.fill_calls == []
    assert verify_password.fill_calls == ["Secret123!"]
    assert create_button.click_calls == 1


def test_do_create_account_signs_in_when_workday_returns_to_login_gate():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    email = _ActionItem()
    password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(email),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(email),
            ("textbox", "Password"): _ActionLocator.single(password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
        url="https://example.test/login",
    )

    with (
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            return_value={"auth_state": "sign_in_gate"},
        ),
        mock.patch.object(mod, "_do_sign_in", return_value=True) as sign_in,
    ):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is True

    sign_in.assert_called_once_with(page, "jerrisonli@gmail.com", "Secret123!")


def test_do_sign_in_opens_email_entrypoint_before_filling_fields():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _EmailEntrypointAuthPage()

    with mock.patch.object(mod, "_is_application_page", return_value=True):
        assert mod._do_sign_in(page, "jerrisonli@gmail.com", "Secret123!") is True

    assert page.sign_in_with_email.click_calls == 1
    assert page.email.fill_calls == ["jerrisonli@gmail.com"]
    assert page.password.fill_calls == ["Secret123!"]
    assert page.sign_in_button.click_calls == 1


def test_do_sign_in_uses_workday_submit_button_selector():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _EmailEntrypointAuthPage()

    with (
        mock.patch.object(mod, "_click_workday_button", return_value=True) as click_button,
        mock.patch.object(mod, "_is_application_page", side_effect=[False, True]),
    ):
        assert mod._do_sign_in(page, "jerrisonli@gmail.com", "Secret123!") is True

    click_button.assert_called_once_with(
        page,
        "[data-automation-id='signInSubmitButton'], button:has-text('Sign In')",
    )


def test_do_password_reset_opens_email_entrypoint_before_forgot_password_lookup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _EmailEntrypointAuthPage()

    with (
        mock.patch.object(mod, "_extract_workday_auth_markers", return_value={"auth_state": "sign_in_gate"}),
        mock.patch.object(mod, "_fetch_workday_email_link", return_value=None),
    ):
        assert mod._do_password_reset(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert page.sign_in_with_email.click_calls == 1
    assert page.forgot_button.click_calls == 1
    assert page.email.fill_calls == ["jerrisonli@gmail.com"]


def test_open_workday_create_account_clicks_link_when_sign_in_shell_is_misclassified():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    create_account_link = _DummyElement("Create Account")
    page = _DummyPage(
        locators={
            "[data-automation-id='signInContent']": _DummyLocator([_DummyElement("sign in shell")]),
            "a:has-text('Create Account'), button:has-text('Create Account')": _DummyLocator([create_account_link]),
        },
        body_text="current step 1 of 7 Create Account/Sign In Sign In Create Account",
    )
    page.url = "https://example.test/login"

    with mock.patch.object(mod, "_extract_workday_auth_markers", return_value={"auth_state": "create_account_gate"}):
        assert mod._open_workday_create_account(page) is True

    assert create_account_link.click_calls == 1


def test_open_workday_create_account_opens_email_entrypoint_before_link_lookup():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _EmailEntrypointAuthPage()

    with mock.patch.object(mod, "_extract_workday_auth_markers", return_value={"auth_state": "sign_in_gate"}):
        assert mod._open_workday_create_account(page) is True

    assert page.sign_in_with_email.click_calls == 1
    assert page.create_account_link.click_calls == 1


def test_open_workday_sign_in_clicks_link_when_create_account_form_is_misclassified():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    sign_in_link = _DummyElement("Sign In")
    page = _DummyPage(
        locators={
            "[data-automation-id='verifyPassword']": _DummyLocator([_DummyElement("verify password")]),
            "button:has-text('Sign In'), a:has-text('Sign In')": _DummyLocator([sign_in_link]),
        },
        body_text="Create Account Sign In Verify New Password",
    )
    page.url = "https://example.test/login"

    with mock.patch.object(mod, "_extract_workday_auth_markers", return_value={"auth_state": "sign_in_gate"}):
        assert mod._open_workday_sign_in(page) is True

    assert sign_in_link.click_calls == 1


def test_do_create_account_follows_account_verification_email_link_when_prompted():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    email = _ActionItem()
    password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    resend_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(email),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
            "button:has-text('Resend Account Verification'), a:has-text('Resend Account Verification')": _ActionLocator.single(
                resend_button
            ),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(email),
            ("textbox", "Password"): _ActionLocator.single(password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
            ("button", "Resend Account Verification"): _ActionLocator.single(resend_button),
        },
        url="https://example.test/login",
    )

    with (
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            side_effect=[
                {
                    "auth_state": "account_verification_gate",
                    "page_url": "https://example.test/login",
                    "heading_text": "Sign In",
                    "alert_text": "Verify your account before you sign in or request a verification email.",
                    "visible_actions": ["Resend Account Verification", "Sign In"],
                    "page_text_excerpt": "Verify your account",
                },
                {
                    "auth_state": "sign_in_gate",
                    "page_url": "https://example.test/login",
                    "heading_text": "Sign In",
                    "alert_text": "",
                    "visible_actions": ["Sign In"],
                    "page_text_excerpt": "Sign In",
                },
            ],
        ),
        mock.patch.object(
            mod,
            "_fetch_workday_account_verification_link",
            return_value="https://example.test/verify-account/token",
        ) as fetch_verification_link,
        mock.patch.object(mod, "_do_sign_in", return_value=True) as sign_in,
    ):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is True

    fetch_verification_link.assert_called_once()
    assert resend_button.click_calls == 0
    assert page.goto_calls == ["https://example.test/verify-account/token"]
    sign_in.assert_called_once_with(page, "jerrisonli@gmail.com", "Secret123!")


def test_complete_workday_account_verification_prefers_existing_email_before_resend():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    resend_button = _ActionItem()
    started_at = datetime(2026, 4, 5, 8, 0, tzinfo=UTC)
    page = _ActionPage(
        locators={
            "button:has-text('Resend Account Verification'), a:has-text('Resend Account Verification')": _ActionLocator.single(
                resend_button
            ),
        },
        role_locators={
            ("button", "Resend Account Verification"): _ActionLocator.single(resend_button),
        },
        url="https://example.test/login",
    )

    with (
        mock.patch.object(
            mod,
            "_fetch_workday_account_verification_link",
            return_value="https://example.test/verify-account/token",
        ) as fetch_verification_link,
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            return_value={
                "auth_state": "sign_in_gate",
                "page_url": "https://example.test/login/ok?redirect=%2Fapply%2FapplyManually",
                "heading_text": "Sign In",
                "alert_text": "",
                "visible_actions": ["Sign In"],
                "page_text_excerpt": "Sign In",
            },
        ),
        mock.patch.object(mod, "_do_sign_in", return_value=True) as sign_in,
    ):
        assert (
            mod._complete_workday_account_verification(
                page,
                "jerrisonli@gmail.com",
                "Secret123!",
                verification_started_at=started_at,
                preferred_job_url="https://example.test/job/example/apply/applyManually",
            )
            is True
        )

    fetch_verification_link.assert_called_once_with(
        min_received_at_utc=started_at,
        wait_seconds=120,
        preferred_job_url="https://example.test/job/example/apply/applyManually",
    )
    assert resend_button.click_calls == 0
    assert page.goto_calls == ["https://example.test/verify-account/token"]
    sign_in.assert_called_once_with(page, "jerrisonli@gmail.com", "Secret123!")


def test_workday_resume_already_uploaded_detects_visible_uploaded_resume():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        body_text="Resume/CV Jerrison Li Resume - Turo Inc..pdf Successfully Uploaded! Save and Continue",
    )

    assert mod._workday_resume_already_uploaded(page, "Jerrison Li Resume - Turo Inc..pdf") is True


def test_workday_resume_already_uploaded_detects_existing_delete_controls():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "[data-automation-id='delete-file']": _DummyLocator(
                [
                    _DummyElement(aria_label="Delete Jerrison Li Resume - Turo Inc..pdf"),
                    _DummyElement(aria_label="Delete Jerrison Li Resume - Turo Inc..pdf"),
                ]
            )
        },
        body_text="",
    )

    assert mod._workday_resume_already_uploaded(page, "Jerrison Li Resume - Turo Inc..pdf") is True


def test_dedupe_workday_uploaded_resume_items_clicks_all_extra_delete_buttons():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    buttons = [_DummyElement(aria_label="Delete Jerrison Li Resume - Turo Inc..pdf") for _ in range(5)]
    page = _DummyPage(
        locators={"[data-automation-id='delete-file']": _DummyLocator(buttons)},
        body_text="",
    )

    deleted = mod._dedupe_workday_uploaded_resume_items(page, "Jerrison Li Resume - Turo Inc..pdf", keep=1)

    assert deleted == 4
    assert buttons[0].click_calls == 0
    assert [button.click_calls for button in buttons[1:]] == [1, 1, 1, 1]


def test_dedupe_workday_uploaded_resume_items_removes_stale_resume_but_keeps_cover_letter():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    stale_resume = _DummyElement(aria_label="Delete Jerrison Li Resume - Principal Product Manager page.pdf")
    current_resume = _DummyElement(aria_label="Delete Jerrison Li Resume - Autodesk Inc..pdf")
    cover_letter = _DummyElement(aria_label="Delete Jerrison Li Cover Letter - Autodesk Inc..pdf")
    page = _DummyPage(
        locators={"[data-automation-id='delete-file']": _DummyLocator([stale_resume, current_resume, cover_letter])},
        body_text="",
    )

    deleted = mod._dedupe_workday_uploaded_resume_items(page, "Jerrison Li Resume - Autodesk Inc..pdf", keep=1)

    assert deleted == 1
    assert stale_resume.click_calls == 1
    assert current_resume.click_calls == 0
    assert cover_letter.click_calls == 0


def test_fill_my_experience_skips_resume_upload_when_current_resume_is_already_listed(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _FileInputItem:
        def __init__(self) -> None:
            self.set_input_files_calls: list[str] = []

        def set_input_files(self, value: str) -> None:
            self.set_input_files_calls.append(value)

    class _Locator:
        def __init__(self, items: list[object] | None = None) -> None:
            self.items = items or []

        def count(self) -> int:
            return len(self.items)

        @property
        def first(self) -> "_Locator":
            return _Locator(self.items[:1])

        def nth(self, index: int) -> "_Locator":
            return _Locator([self.items[index]])

        def set_input_files(self, value: str) -> None:
            if not self.items:
                raise RuntimeError("no file input")
            self.items[0].set_input_files(value)

    class _Page:
        def __init__(self, file_input_item: _FileInputItem) -> None:
            self.file_input_item = file_input_item

        def locator(self, selector: str) -> _Locator:
            if selector == "input[type='file'][data-automation-id='file-upload-input-ref'], input[type='file']":
                return _Locator([self.file_input_item])
            return _Locator()

        def wait_for_timeout(self, timeout_ms: int) -> None:
            return None

        def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return "Resume/CV Jerrison Li Resume - Turo Inc..pdf Successfully Uploaded!"

    file_input_item = _FileInputItem()
    page = _Page(file_input_item)
    out_dir = tmp_path
    resume_path = out_dir / "Jerrison Li Resume - Turo Inc..pdf"
    resume_path.write_text("resume", encoding="utf-8")

    with (
        mock.patch.object(mod, "find_resume_file", return_value=resume_path),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=[]),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert file_input_item.set_input_files_calls == []
    assert filled == []


def test_fill_my_experience_prunes_stale_resume_when_current_resume_is_already_listed(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    class _FileInputItem:
        def __init__(self) -> None:
            self.set_input_files_calls: list[str] = []

        def set_input_files(self, value: str) -> None:
            self.set_input_files_calls.append(value)

    class _Locator:
        def __init__(self, items: list[object] | None = None) -> None:
            self.items = items or []

        def count(self) -> int:
            return len(self.items)

        @property
        def first(self) -> "_Locator":
            return _Locator(self.items[:1])

        def nth(self, index: int):
            return self.items[index]

        def set_input_files(self, value: str) -> None:
            if not self.items:
                raise RuntimeError("no file input")
            self.items[0].set_input_files(value)

    class _Page:
        def __init__(self, file_input_item: _FileInputItem, delete_buttons: list[object]) -> None:
            self.file_input_item = file_input_item
            self.delete_buttons = delete_buttons

        def locator(self, selector: str):
            if selector == "input[type='file'][data-automation-id='file-upload-input-ref'], input[type='file']":
                return _Locator([self.file_input_item])
            if selector == "[data-automation-id='delete-file']":
                return _Locator(self.delete_buttons)
            return _Locator()

        def wait_for_timeout(self, timeout_ms: int) -> None:
            return None

        def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return ""

    file_input_item = _FileInputItem()
    stale_resume = _DummyElement(aria_label="Delete Jerrison Li Resume - Principal Product Manager page.pdf")
    current_resume = _DummyElement(aria_label="Delete Jerrison Li Resume - Hewlett Packard Enterprise.pdf")
    page = _Page(file_input_item, [stale_resume, current_resume])
    out_dir = tmp_path
    resume_path = out_dir / "Jerrison Li Resume - Hewlett Packard Enterprise.pdf"
    resume_path.write_text("resume", encoding="utf-8")

    with (
        mock.patch.object(mod, "find_resume_file", return_value=resume_path),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=[]),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert stale_resume.click_calls == 1
    assert current_resume.click_calls == 0
    assert file_input_item.set_input_files_calls == []
    assert filled == []


def test_build_workday_education_entries_uses_profile_names_and_resume_years():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    profile = mod.parse_application_profile(
        """
# Application Profile
## Work Authorization
- Country: United States
- Location: San Francisco, CA
- Work Authorization Statement: I am always authorized to work in the United States unconditionally.
- Authorized to Work Unconditionally: Yes
- Require Sponsorship Now: No
- Require Sponsorship in Future: No
- Sponsorship Answer: No
## Voluntary Self Identification
- Gender: Male
- Race or Ethnicity: Hispanic or Latino
- Veteran Status: I am not a protected veteran
- Disability Status: No, I do not have a disability and have not had one in the past
- Sexual Orientation: Straight / Heterosexual
## Education
- The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)
- Penn Engineering, University of Pennsylvania; Master of Science in Computer Science
        """.strip()
    )
    resume_lines = [
        "JERRISON LI",
        "EXPERIENCE",
        "MOODY'S ANALYTICS — Associate Director, Product Management",
        "San Francisco, CA | August 2024–Present",
        "EDUCATION",
        "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "MBA (Finance) | Philadelphia, PA | 2018–2020",
        "PENN ENGINEERING, UNIVERSITY OF PENNSYLVANIA",
        "M.S. Computer Science | Philadelphia, PA | 2018–2020",
        "SKILLS & ADDITIONAL",
    ]

    entries = mod._build_workday_education_entries(profile, resume_lines)

    assert len(entries) == 2
    assert entries[0].school == "The Wharton School, University of Pennsylvania"
    assert entries[0].degree_text == "Master of Business Administration (M.B.A.)"
    assert entries[0].start_year == "2018"
    assert entries[0].end_year == "2020"
    assert entries[0].degree_candidates[0] == "Master of Business Administration (M.B.A.)"
    assert entries[0].discipline_candidates[:2] == ["Finance", "Business Administration"]
    assert entries[1].school == "Penn Engineering, University of Pennsylvania"
    assert entries[1].degree_candidates[0] == "MS"
    assert entries[1].discipline_candidates[0] == "Computer Science"


def test_workday_education_degree_candidates_include_bucket_dropdown_labels():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert "Masters" in mod._workday_education_degree_candidates("Master of Business Administration (M.B.A.)")
    assert "MBA" in mod._workday_education_degree_candidates("Master of Business Administration (M.B.A.)")
    assert "MS" in mod._workday_education_degree_candidates("Master of Science in Computer Science")
    assert "Bachelors" in mod._workday_education_degree_candidates("Bachelor of Science in Computer Science")
    assert "BS" in mod._workday_education_degree_candidates("Bachelor of Science in Computer Science")
    assert "Associates" in mod._workday_education_degree_candidates("Associate of Science")
    assert "AS" in mod._workday_education_degree_candidates("Associate of Science")


def test_fill_workday_dropdown_locator_retries_when_selected_chips_mask_live_options():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    dropdown = _DropdownFieldLocator(value="Select One", field_id="education-1--degree")
    selected_chip = _PromptOptionItem(
        "Finance",
        data_automation_id="selectedItem",
        selected_chip=True,
    )
    live_option = _PromptOptionItem("Masters", on_click=lambda: setattr(dropdown, "value", "Masters"))
    page = _PromptOptionsPage(
        dropdown=dropdown,
        option_batches=[
            [selected_chip],
            [selected_chip, live_option],
        ],
    )

    filled = mod._fill_workday_dropdown_locator(page, dropdown, "Masters")

    assert filled is True
    assert selected_chip.click_calls == 0
    assert live_option.click_calls == 1
    assert dropdown.value == "Masters"


def test_fill_workday_dropdown_locator_maps_specific_degree_to_generic_bucket_option():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    dropdown = _DropdownFieldLocator(value="Select One", field_id="education-1--degree")
    live_option = _PromptOptionItem("Masters", on_click=lambda: setattr(dropdown, "value", "Masters"))
    page = _PromptOptionsPage(
        dropdown=dropdown,
        option_batches=[[live_option]],
    )

    filled = mod._fill_workday_dropdown_locator(page, dropdown, "Master of Business Administration (M.B.A.)")

    assert filled is True
    assert live_option.click_calls == 1
    assert dropdown.value == "Masters"


def test_select_workday_prompt_option_via_input_ignores_selected_value_chips():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    input_locator = _WorkdayPromptInput()
    selected_chip = _PromptOptionItem(
        "Finance",
        data_automation_id="selectedItem",
        selected_chip=True,
    )
    live_option = _PromptOptionItem("Finance")
    page = _PromptOptionsPage(
        input_locator=input_locator,
        option_batches=[[selected_chip, live_option]],
        input_selector='[id="education-1--fieldOfStudy"]',
    )

    filled = mod._select_workday_prompt_option_via_input(page, '[id="education-1--fieldOfStudy"]', "Finance")

    assert filled is True
    assert selected_chip.click_calls == 0
    assert live_option.click_calls == 1


def test_fill_my_experience_populates_workday_education_fields_from_profile_and_resume(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    school = _FieldLocator(field_id="education-1--schoolName")
    degree = _FieldLocator(value="Select One", field_id="education-1--degree")
    field_of_study = _FieldLocator(field_id="education-1--fieldOfStudy")
    first_year = _FieldLocator(field_id="education-1--firstYearAttended")
    last_year = _FieldLocator(field_id="education-1--lastYearAttended")
    page = _MyExperiencePage(
        [
            _EducationRow(
                school=school,
                degree=degree,
                field_of_study=field_of_study,
                first_year=first_year,
                last_year=last_year,
            )
        ]
    )
    out_dir = tmp_path

    profile = mod.parse_application_profile(
        """
# Application Profile
## Work Authorization
- Country: United States
- Location: San Francisco, CA
- Work Authorization Statement: I am always authorized to work in the United States unconditionally.
- Authorized to Work Unconditionally: Yes
- Require Sponsorship Now: No
- Require Sponsorship in Future: No
- Sponsorship Answer: No
## Voluntary Self Identification
- Gender: Male
- Race or Ethnicity: Hispanic or Latino
- Veteran Status: I am not a protected veteran
- Disability Status: No, I do not have a disability and have not had one in the past
- Sexual Orientation: Straight / Heterosexual
## Education
- The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)
        """.strip()
    )
    resume_lines = [
        "JERRISON LI",
        "EXPERIENCE",
        "MOODY'S ANALYTICS — Associate Director, Product Management",
        "San Francisco, CA | August 2024–Present",
        "EDUCATION",
        "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "MBA (Finance) | Philadelphia, PA | 2018–2020",
        "SKILLS & ADDITIONAL",
    ]
    degree_attempts: list[str] = []
    study_attempts: list[tuple[str, str]] = []

    def _fake_human_fill(locator, value: str, delay_ms: int = 0) -> None:
        del delay_ms
        locator.fill(value)

    def _fake_fill_dropdown_locator(current_page, dropdown, value: str) -> bool:
        del current_page
        degree_attempts.append(value)
        dropdown.value = value
        return value == "Master of Business Administration (M.B.A.)"

    def _fake_select_prompt_option(current_page, selector: str, expected_text: str, *, label_text: str | None = None) -> bool:
        del current_page, label_text
        study_attempts.append((selector, expected_text))
        return expected_text == "Finance"

    with (
        mock.patch.object(mod, "find_resume_file", return_value=None),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=resume_lines),
        mock.patch.object(mod, "parse_application_profile", return_value=profile),
        mock.patch.object(mod, "human_fill", side_effect=_fake_human_fill),
        mock.patch.object(mod, "_fill_workday_dropdown_locator", side_effect=_fake_fill_dropdown_locator),
        mock.patch.object(mod, "_select_workday_prompt_option_via_input", side_effect=_fake_select_prompt_option),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert school.value == "The Wharton School, University of Pennsylvania"
    assert degree_attempts == ["Master of Business Administration (M.B.A.)"]
    assert study_attempts == [('[id="education-1--fieldOfStudy"]', "Finance")]
    assert first_year.value == "2018"
    assert last_year.value == "2020"
    assert {item["field_name"] for item in filled} >= {
        "education_1_school",
        "education_1_degree",
        "education_1_field_of_study",
        "education_1_start_year",
        "education_1_end_year",
    }


def test_fill_my_experience_corrects_prefilled_start_date_and_populates_role_description_from_resume(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    job_title = _FieldLocator(value="Associate Director, Product Management", field_id="workExperience-1--jobTitle")
    company = _FieldLocator(value="MOODY'S ANALYTICS", field_id="workExperience-1--companyName")
    location = _FieldLocator(value="San Francisco, CA", field_id="workExperience-1--location")
    current_checkbox = _CheckboxLocator(checked=True, field_id="workExperience-1--currentlyWorkHere")
    start_month = _FieldLocator(value="2", field_id="workExperience-1--startDate-dateSectionMonth-input")
    start_year = _FieldLocator(value="2024", field_id="workExperience-1--startDate-dateSectionYear-input")
    role_description = _FieldLocator(field_id="workExperience-1--roleDescription")
    page = _MyExperiencePage(
        [],
        work_experience_rows=[
            _WorkExperienceRow(
                job_title=job_title,
                company=company,
                location=location,
                current_checkbox=current_checkbox,
                start_month=start_month,
                start_year=start_year,
                role_description=role_description,
            )
        ],
    )
    out_dir = tmp_path
    resume_lines = [
        "JERRISON LI",
        "EXPERIENCE",
        "MOODY'S ANALYTICS — Associate Director, Product Management",
        "San Francisco, CA | August 2024–Present",
        "* Drove 166% YoY client growth for UnderwriteIQ catastrophe and cyber risk modeling platform.",
        "* Launched SlipStream agentic AI system transforming unstructured policy documents into structured data.",
        "KYTE — Staff Product Manager (Series B, On-Demand Car Rental)",
        "San Francisco, CA | Mar 2022–August 2024",
        "* Built company's first ML risk engine from 0-to-1.",
        "EDUCATION",
        "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "MBA (Finance) | Philadelphia, PA | 2018–2020",
        "SKILLS & ADDITIONAL",
    ]

    def _fake_human_fill(locator, value: str, delay_ms: int = 0) -> None:
        del delay_ms
        locator.fill(value)

    with (
        mock.patch.object(mod, "find_resume_file", return_value=None),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=resume_lines),
        mock.patch.object(mod, "parse_application_profile", return_value=None),
        mock.patch.object(mod, "human_fill", side_effect=_fake_human_fill),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert start_month.value == "8"
    assert start_year.value == "2024"
    assert "Drove 166% YoY client growth" in role_description.value
    assert "Launched SlipStream agentic AI system" in role_description.value
    assert {item["field_name"] for item in filled} >= {
        "work_experience_1_start_date",
        "work_experience_1_role_description",
    }


def test_fill_my_experience_populates_source_backed_language_and_skills(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = mock.Mock()
    page.locator.return_value = _EmptyLocator()
    page.wait_for_timeout.return_value = None
    page.evaluate.return_value = None
    out_dir = tmp_path
    resume_lines = [
        "JERRISON LI",
        "SKILLS & ADDITIONAL",
        "Technical: Python, SQL, TypeScript, Figma | ML/AI: Snowflake Cortex, LLM orchestration, RAG systems, GLMs | Data: A/B testing, analytics pipelines",
        "Languages: Spanish (native), Cantonese (native), Mandarin (advanced)",
    ]
    profile = mod.parse_application_profile(
        """
# Application Profile
## Work Authorization
- Country: United States
- Location: San Francisco, CA
- Work Authorization Statement: I am always authorized to work in the United States unconditionally.
- Authorized to Work Unconditionally: Yes
- Require Sponsorship Now: No
- Require Sponsorship in Future: No
- Sponsorship Answer: No
## Voluntary Self Identification
- Gender: Male
- Race or Ethnicity: Hispanic or Latino
- Veteran Status: I am not a protected veteran
- Disability Status: No, I do not have a disability and have not had one in the past
- Sexual Orientation: Straight / Heterosexual
## Languages
- Languages Spoken: English, Spanish, Mandarin, Cantonese
        """.strip()
    )
    dropdown_attempts: list[tuple[str, str, str | None]] = []
    checkbox_attempts: list[str] = []
    prompt_attempts: list[tuple[str, list[str]]] = []

    def _fake_fill_labeled_dropdown(_page, label_text: str, value: str, *, profile_field: str | None = None) -> bool:
        dropdown_attempts.append((label_text, value, profile_field))
        return True

    def _fake_check_checkbox(_page, label_text: str) -> bool:
        checkbox_attempts.append(label_text)
        return True

    def _fake_fill_prompt_field(_page, label_text: str, candidates) -> str | None:
        prompt_attempts.append((label_text, list(candidates)))
        return "Python"

    with (
        mock.patch.object(mod, "find_resume_file", return_value=None),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=resume_lines),
        mock.patch.object(mod, "parse_application_profile", return_value=profile),
        mock.patch.object(mod, "_fill_workday_labeled_dropdown", side_effect=_fake_fill_labeled_dropdown),
        mock.patch.object(mod, "_check_workday_checkbox_for_label", side_effect=_fake_check_checkbox),
        mock.patch.object(mod, "_fill_workday_prompt_field", side_effect=_fake_fill_prompt_field, create=True),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert dropdown_attempts[:2] == [
        ("Language", "Spanish", None),
        ("Level", "C2", None),
    ]
    assert checkbox_attempts == ["I am fluent in this language."]
    assert prompt_attempts
    assert prompt_attempts[0][0] == "Type to Add Skills"
    assert "Python" in prompt_attempts[0][1]
    assert "SQL" in prompt_attempts[0][1]
    assert {item["field_name"] for item in filled} >= {
        "language_1",
        "language_1_level",
        "language_1_fluent",
        "skills",
    }


def test_workday_language_level_candidates_cover_cefr_style_dropdown_values():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert mod._workday_language_level_candidates("native")[:3] == [
        "C2",
        "Proficient/Native Speaker",
        "Native",
    ]
    assert mod._workday_language_level_candidates("advanced")[:2] == [
        "C1",
        "Advanced",
    ]


def test_fill_my_experience_adds_missing_workday_education_rows_before_filling(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = _MyExperiencePage([], selector_items={})
    pending_rows = [
        _EducationRow(
            school=_FieldLocator(field_id="education-1--schoolName"),
            degree=_FieldLocator(value="Select One", field_id="education-1--degree"),
            field_of_study=_FieldLocator(field_id="education-1--fieldOfStudy"),
            first_year=_FieldLocator(field_id="education-1--firstYearAttended"),
            last_year=_FieldLocator(field_id="education-1--lastYearAttended"),
        ),
        _EducationRow(
            school=_FieldLocator(field_id="education-2--schoolName"),
            degree=_FieldLocator(value="Select One", field_id="education-2--degree"),
            field_of_study=_FieldLocator(field_id="education-2--fieldOfStudy"),
            first_year=_FieldLocator(field_id="education-2--firstYearAttended"),
            last_year=_FieldLocator(field_id="education-2--lastYearAttended"),
        ),
    ]

    def _add_education_row() -> None:
        if pending_rows:
            page._education_rows.append(pending_rows.pop(0))

    add_button = _ClickableItem(on_click=_add_education_row)
    page._selector_items = {
        "div[role='group'][aria-labelledby='Education-section'] button[data-automation-id='add-button']": [add_button],
        "button[data-automation-id='add-button']:has-text('Add Another')": [add_button],
    }
    out_dir = tmp_path

    profile = mod.parse_application_profile(
        """
# Application Profile
## Work Authorization
- Country: United States
- Location: San Francisco, CA
- Work Authorization Statement: I am always authorized to work in the United States unconditionally.
- Authorized to Work Unconditionally: Yes
- Require Sponsorship Now: No
- Require Sponsorship in Future: No
- Sponsorship Answer: No
## Voluntary Self Identification
- Gender: Male
- Race or Ethnicity: Hispanic or Latino
- Veteran Status: I am not a protected veteran
- Disability Status: No, I do not have a disability and have not had one in the past
- Sexual Orientation: Straight / Heterosexual
## Education
- The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)
- Penn Engineering, University of Pennsylvania; Master of Science in Computer Science
        """.strip()
    )
    resume_lines = [
        "JERRISON LI",
        "EXPERIENCE",
        "MOODY'S ANALYTICS — Associate Director, Product Management",
        "San Francisco, CA | August 2024–Present",
        "EDUCATION",
        "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "MBA (Finance) | Philadelphia, PA | 2018–2020",
        "PENN ENGINEERING, UNIVERSITY OF PENNSYLVANIA",
        "M.S. Computer Science | Philadelphia, PA | 2018–2020",
        "SKILLS & ADDITIONAL",
    ]
    degree_attempts: list[str] = []

    def _fake_human_fill(locator, value: str, delay_ms: int = 0) -> None:
        del delay_ms
        locator.fill(value)

    def _fake_fill_dropdown_locator(current_page, dropdown, value: str) -> bool:
        del current_page
        degree_attempts.append(value)
        dropdown.value = value
        return True

    def _fake_select_prompt_option(current_page, selector: str, expected_text: str, *, label_text: str | None = None) -> bool:
        del current_page, selector, label_text
        return bool(expected_text)

    with (
        mock.patch.object(mod, "find_resume_file", return_value=None),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=resume_lines),
        mock.patch.object(mod, "parse_application_profile", return_value=profile),
        mock.patch.object(mod, "human_fill", side_effect=_fake_human_fill),
        mock.patch.object(mod, "_fill_workday_dropdown_locator", side_effect=_fake_fill_dropdown_locator),
        mock.patch.object(mod, "_select_workday_prompt_option_via_input", side_effect=_fake_select_prompt_option),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert len(page._education_rows) == 2
    assert add_button.click_calls == 2
    assert page._education_rows[0].locators["[data-fkit-id$='--schoolName'] input"].value == (
        "The Wharton School, University of Pennsylvania"
    )
    assert page._education_rows[1].locators["[data-fkit-id$='--schoolName'] input"].value == (
        "Penn Engineering, University of Pennsylvania"
    )
    assert {item["field_name"] for item in filled} >= {
        "education_1_school",
        "education_2_school",
        "education_1_degree",
        "education_2_degree",
    }


def test_fill_my_experience_adds_missing_workday_work_experience_rows_before_filling(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    page = _MyExperiencePage([], selector_items={})
    pending_rows = [
        _WorkExperienceRow(
            job_title=_FieldLocator(field_id="workExperience-1--jobTitle"),
            company=_FieldLocator(field_id="workExperience-1--companyName"),
            location=_FieldLocator(field_id="workExperience-1--location"),
            current_checkbox=_CheckboxLocator(field_id="workExperience-1--currentlyWorkHere"),
            start_month=_FieldLocator(field_id="workExperience-1--startDate-dateSectionMonth-input"),
            start_year=_FieldLocator(field_id="workExperience-1--startDate-dateSectionYear-input"),
            role_description=_FieldLocator(field_id="workExperience-1--roleDescription"),
        ),
        _WorkExperienceRow(
            job_title=_FieldLocator(field_id="workExperience-2--jobTitle"),
            company=_FieldLocator(field_id="workExperience-2--companyName"),
            location=_FieldLocator(field_id="workExperience-2--location"),
            current_checkbox=_CheckboxLocator(field_id="workExperience-2--currentlyWorkHere"),
            start_month=_FieldLocator(field_id="workExperience-2--startDate-dateSectionMonth-input"),
            start_year=_FieldLocator(field_id="workExperience-2--startDate-dateSectionYear-input"),
            role_description=_FieldLocator(field_id="workExperience-2--roleDescription"),
            end_month=_FieldLocator(field_id="workExperience-2--endDate-dateSectionMonth-input"),
            end_year=_FieldLocator(field_id="workExperience-2--endDate-dateSectionYear-input"),
        ),
    ]

    def _add_work_experience_row() -> None:
        if pending_rows:
            page._work_experience_rows.append(pending_rows.pop(0))

    add_button = _ClickableItem(on_click=_add_work_experience_row)
    page._selector_items = {
        "div[role='group'][aria-labelledby='Work-Experience-section'] button[data-automation-id='add-button']": [
            add_button
        ],
        "div[role='group'][aria-labelledby='Work-Experience-section'] button:has-text('Add Another')": [add_button],
    }
    out_dir = tmp_path
    resume_lines = [
        "JERRISON LI",
        "EXPERIENCE",
        "MOODY'S ANALYTICS — Associate Director, Product Management",
        "San Francisco, CA | August 2024–Present",
        "* Drove 166% YoY client growth for UnderwriteIQ catastrophe and cyber risk modeling platform.",
        "* Launched SlipStream agentic AI system transforming unstructured policy documents into structured data.",
        "KYTE — Staff Product Manager (Series B, On-Demand Car Rental)",
        "San Francisco, CA | Mar 2022–August 2024",
        "* Built company's first ML risk engine from 0-to-1.",
        "EDUCATION",
        "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "MBA (Finance) | Philadelphia, PA | 2018–2020",
        "SKILLS & ADDITIONAL",
    ]

    def _fake_human_fill(locator, value: str, delay_ms: int = 0) -> None:
        del delay_ms
        locator.fill(value)

    with (
        mock.patch.object(mod, "find_resume_file", return_value=None),
        mock.patch.object(mod, "find_cover_letter_file", side_effect=FileNotFoundError),
        mock.patch.object(mod, "_load_workday_resume_lines", return_value=resume_lines),
        mock.patch.object(mod, "parse_application_profile", return_value=None),
        mock.patch.object(mod, "human_fill", side_effect=_fake_human_fill),
    ):
        filled = mod._fill_my_experience(page, out_dir)

    assert len(page._work_experience_rows) == 2
    assert add_button.click_calls == 2
    assert page._work_experience_rows[0].locators["[data-fkit-id$='--jobTitle'] input"].value == (
        "Associate Director, Product Management"
    )
    assert page._work_experience_rows[0].locators["[data-fkit-id$='--companyName'] input"].value == "MOODY'S ANALYTICS"
    assert page._work_experience_rows[0].locators["[data-fkit-id$='--startDate'] input[aria-label='Month']"].value == (
        "8"
    )
    assert page._work_experience_rows[0].locators["[data-fkit-id$='--startDate'] input[aria-label='Year']"].value == (
        "2024"
    )
    assert "Drove 166% YoY client growth" in page._work_experience_rows[0].locators[
        "[data-fkit-id$='--roleDescription'] textarea"
    ].value
    assert page._work_experience_rows[1].locators["[data-fkit-id$='--jobTitle'] input"].value == (
        "Staff Product Manager (Series B, On-Demand Car Rental)"
    )
    assert page._work_experience_rows[1].locators["[data-fkit-id$='--companyName'] input"].value == "KYTE"
    assert page._work_experience_rows[1].locators["[data-fkit-id$='--endDate'] input[aria-label='Month']"].value == (
        "8"
    )
    assert page._work_experience_rows[1].locators["[data-fkit-id$='--endDate'] input[aria-label='Year']"].value == (
        "2024"
    )
    assert {item["field_name"] for item in filled} >= {
        "work_experience_1_job_title",
        "work_experience_1_company",
        "work_experience_1_start_date",
        "work_experience_1_role_description",
        "work_experience_2_job_title",
        "work_experience_2_company",
        "work_experience_2_end_date",
    }


def test_do_create_account_follows_verification_email_when_sign_in_reveals_verification_gate():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    email = _ActionItem()
    password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    resend_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(email),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
            "button:has-text('Resend Account Verification'), a:has-text('Resend Account Verification')": _ActionLocator.single(
                resend_button
            ),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(email),
            ("textbox", "Password"): _ActionLocator.single(password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
            ("button", "Resend Account Verification"): _ActionLocator.single(resend_button),
        },
        url="https://example.test/login",
    )

    with (
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(
            mod,
            "_extract_workday_auth_markers",
            side_effect=[
                {
                    "auth_state": "sign_in_gate",
                    "page_url": "https://example.test/login",
                    "heading_text": "Sign In",
                    "alert_text": "",
                    "visible_actions": ["Sign In"],
                    "page_text_excerpt": "Sign In",
                },
                {
                    "auth_state": "account_verification_gate",
                    "page_url": "https://example.test/login",
                    "heading_text": "Sign In",
                    "alert_text": "Verify your account before you sign in or request a verification email.",
                    "visible_actions": ["Resend Account Verification", "Sign In"],
                    "page_text_excerpt": "Verify your account",
                },
                {
                    "auth_state": "sign_in_gate",
                    "page_url": "https://example.test/login",
                    "heading_text": "Sign In",
                    "alert_text": "",
                    "visible_actions": ["Sign In"],
                    "page_text_excerpt": "Sign In",
                },
            ],
        ),
        mock.patch.object(
            mod,
            "_fetch_workday_account_verification_link",
            return_value="https://example.test/verify-account/token",
        ) as fetch_verification_link,
        mock.patch.object(mod, "_do_sign_in", side_effect=[False, True]) as sign_in,
    ):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is True

    fetch_verification_link.assert_called_once()
    assert resend_button.click_calls == 0
    assert page.goto_calls == ["https://example.test/verify-account/token"]
    assert sign_in.call_count == 2


def test_do_create_account_prefers_last_visible_named_fields_on_combined_auth_shell():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    sign_in_email = _ActionItem()
    create_account_email = _ActionItem()
    sign_in_password = _ActionItem()
    create_account_password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator([sign_in_email, create_account_email]),
            ("textbox", "Password"): _ActionLocator([sign_in_password, create_account_password]),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert sign_in_email.fill_calls == []
    assert create_account_email.fill_calls == ["jerrisonli@gmail.com"]
    assert sign_in_password.fill_calls == []
    assert create_account_password.fill_calls == ["Secret123!"]
    assert verify_password.fill_calls == ["Secret123!"]
    assert create_button.click_calls == 1


def test_do_create_account_ignores_hidden_cookie_checkbox_when_terms_checkbox_is_unnamed():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    email = _ActionItem()
    password = _ActionItem()
    verify_password = _ActionItem()
    hidden_cookie_checkbox = _ActionItem(visible=False)
    visible_terms_checkbox = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(email),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
            "input[type='checkbox']": _ActionLocator([hidden_cookie_checkbox, visible_terms_checkbox]),
        },
        role_locators={
            ("textbox", "Email Address"): _ActionLocator.single(email),
            ("textbox", "Password"): _ActionLocator.single(password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert hidden_cookie_checkbox.check_calls == 0
    assert visible_terms_checkbox.check_calls == 1
    assert create_button.click_calls == 1


def test_do_create_account_fails_closed_when_only_hidden_email_locator_is_available():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    hidden_email = _ActionItem(visible=False, fail_when_hidden=True)
    password = _ActionItem()
    verify_password = _ActionItem()
    create_button = _ActionItem()
    page = _ActionPage(
        locators={
            "[data-automation-id='email']": _ActionLocator.single(hidden_email),
            "[data-automation-id='password']": _ActionLocator.single(password),
            "[data-automation-id='verifyPassword']": _ActionLocator.single(verify_password),
        },
        role_locators={
            ("textbox", "Password"): _ActionLocator.single(password),
            ("button", "Create Account"): _ActionLocator.single(create_button),
        },
    )

    with mock.patch.object(mod, "_is_application_page", return_value=False):
        assert mod._do_create_account(page, "jerrisonli@gmail.com", "Secret123!") is False

    assert hidden_email.fill_calls == []
    assert password.fill_calls == []
    assert verify_password.fill_calls == []
    assert create_button.click_calls == 0


def test_ensure_workday_application_context_resumes_authenticated_job_page_with_apply():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={"button:has-text('Apply'), a:has-text('Apply')": _DummyLocator([_DummyElement("Apply")])}
    )

    with (
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(mod, "_resume_workday_application", return_value=True) as resume,
        mock.patch.object(mod, "_extract_workday_auth_markers", return_value={"auth_state": "sign_in_gate"}),
    ):
        assert mod._ensure_workday_application_context(page, "https://example.test/job") is True

    resume.assert_called_once_with(page, "https://example.test/job", out_dir=None)


def test_workday_option_text_matches_avoids_false_positive_substrings():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    assert mod._workday_option_text_matches("Male", "Male")
    assert not mod._workday_option_text_matches("Male", "Female")
    assert mod._workday_option_text_matches("No", "No, I do not require sponsorship")
    assert not mod._workday_option_text_matches("No", "Not applicable")
    assert not mod._workday_option_text_matches(
        "Hispanic or Latino",
        "Asian (Not Hispanic or Latino) (United States of America)",
    )


def test_detect_current_page_treats_review_shell_selector_as_review():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "main": _DummyLocator(
                elements=[_DummyElement("main")],
                children={"h2, h3": _DummyLocator([_DummyElement("One last check before submit")])},
            ),
            "[data-automation-id='applyFlowReviewPage']": _DummyLocator([_DummyElement("review shell")]),
        },
        role_locators={
            ("button", "Submit"): _DummyLocator([_DummyElement("Submit")]),
        },
        body_text="Review and submit your application.",
    )

    assert mod._detect_current_page(page) == mod.PAGE_REVIEW


def test_is_application_page_rejects_candidate_home_dashboard_shell():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "button:has-text('jerrisonli@gmail.com'), button:has-text('Settings')": _DummyLocator(
                [_DummyElement("jerrisonli@gmail.com")]
            ),
            "main": _DummyLocator(
                elements=[_DummyElement("main")],
                children={"h2": _DummyLocator([_DummyElement("Candidate Home")])},
            ),
        },
        body_text="Candidate Home My Applications My Tasks Search for Jobs Job Alerts",
    )
    page.url = "https://hpe.wd5.myworkdayjobs.com/en-US/Careers"

    assert mod._is_application_page(page) is False


def test_is_application_page_accepts_hpe_form_with_candidate_home_header_chrome():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "button:has-text('jerrisonli@gmail.com'), button:has-text('Settings')": _DummyLocator(
                [_DummyElement("Settings")]
            ),
            "main": _DummyLocator(
                elements=[_DummyElement("main")],
                children={
                    "h2": _DummyLocator([_DummyElement("My Information")]),
                    "h2, h3": _DummyLocator([_DummyElement("My Information")]),
                },
            ),
        },
        role_locators={
            ("button", "Save and Continue"): _DummyLocator([_DummyElement("Save and Continue")]),
            ("textbox", "First Name"): _DummyLocator([_DummyElement("Jerrison")]),
        },
        body_text=(
            "Candidate Home Search for Jobs Back to Job Posting Product Manager Principal "
            "current step 1 of 6 My Information First Name Last Name Save and Continue"
        ),
    )
    page.url = (
        "https://hpe.wd5.myworkdayjobs.com/en-US/Jobsathpe/job/"
        "Santa-Clara%2C-California%2C-United-States-of-America/"
        "Product-Manager-Principal_1198554/apply"
    )

    assert mod._is_application_page(page) is True


def test_detect_current_page_treats_future_opportunities_form_as_my_information():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    body_text = ("intro " * 800) + "resume/cv upload successfully uploaded"
    page = _DummyPage(
        locators={
            "main": _DummyLocator(
                elements=[_DummyElement("main")],
                children={"h2, h3": _DummyLocator([_DummyElement("Apply for Future Opportunities")])},
            ),
        },
        body_text=body_text,
    )

    assert mod._detect_current_page(page) == mod.PAGE_MY_INFO


def test_detect_current_page_treats_sign_in_shell_progress_labels_as_create_account():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "[data-automation-id='signInContent']": _DummyLocator([_DummyElement("sign in shell")]),
        },
        body_text=(
            "current step 1 of 7 Create Account/Sign In "
            "step 2 of 7 My Information "
            "step 6 of 7 Voluntary Disclosures "
            "step 7 of 7 Review "
            "Sign In Create Account Forgot your password?"
        )
    )
    page.url = "https://servicetitan.wd1.myworkdayjobs.com/en-US/ServiceTitan/job/example/apply/applyManually"

    assert mod._detect_current_page(page) == mod.PAGE_CREATE_ACCOUNT


def test_is_workday_review_shell_detects_submit_button_with_review_heading():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    page = _DummyPage(
        locators={
            "main": _DummyLocator(
                elements=[_DummyElement("main")],
                children={"h2, h3": _DummyLocator([_DummyElement("Review")])},
            ),
            "button[data-automation-id='submitButton']": _DummyLocator([_DummyElement("Submit Application")]),
        },
        body_text="Please review your application before submitting.",
    )

    assert mod._is_workday_review_shell(page) is True


def test_click_next_button_ignores_submit_buttons():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    submit_button = _DummyElement("Submit")
    page = _DummyPage(
        role_locators={
            ("button", "Submit"): _DummyLocator([submit_button]),
        }
    )

    assert mod._click_next_button(page) is False
    assert submit_button.click_calls == 0


def test_fill_my_information_checks_generic_acknowledgment_checkbox():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    profile = SimpleNamespace(email="", phone="", location="")
    application_profile = SimpleNamespace(how_did_you_hear="")
    page = mock.Mock()
    page.evaluate.return_value = None
    page.locator.return_value = _EmptyLocator()
    page.get_by_role.return_value = _EmptyLocator()
    page.wait_for_timeout.return_value = None

    with mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=True):
        filled = mod._fill_my_information(page, profile, application_profile)

    assert any(entry["field_name"] == "required_acknowledgment_checked" for entry in filled)


def test_fill_my_information_follows_nested_workday_source_options_before_trying_other_top_level_candidates():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    profile = SimpleNamespace(email="", phone="", location="")
    application_profile = SimpleNamespace(how_did_you_hear="")
    source_button = _DummyLocator([_DummyElement("How Did You Hear About Us")])
    page = mock.Mock()
    page.locator.return_value = _EmptyLocator()
    page.wait_for_timeout.return_value = None
    page.keyboard = mock.Mock()

    def _get_by_role(role: str, name: str):
        if role == "button" and name == "How Did You Hear About Us":
            return source_button
        return _EmptyLocator()

    page.get_by_role.side_effect = _get_by_role

    def _evaluate(script: str, *_args):
        if "const allFields = document.querySelectorAll" in script:
            return "input_found"
        if "const allOpts = document.querySelectorAll" in script:
            return {
                "sourceOpts": ["Job Board", "Adobe Source"],
                "otherOpts": 0,
                "total": 2,
            }
        return None

    page.evaluate.side_effect = _evaluate

    attempts: list[str] = []

    def _record_input_attempt(_page, _selector: str, candidate: str, *, label_text: str | None = None) -> bool:
        del _page, _selector, label_text
        attempts.append(candidate)
        return candidate == "TrueUp"

    def _record_locator_attempt(_page, _label_text: str, candidate: str) -> str | None:
        del _page, _label_text
        attempts.append(candidate)
        if candidate == "Job Board":
            return "Job Board"
        return None

    def _source_candidates(
        _label: str,
        options: list[str],
        *,
        source_hint: str = "",
        source_answer: str = "",
        company_name: str = "",
    ) -> list[str]:
        del source_hint, source_answer, company_name
        if options == ["Job Board", "Adobe Source"]:
            return ["Job Board", "Adobe Source"]
        if options == ["TrueUp", "LinkedIn"]:
            return ["TrueUp"]
        return []

    def _state_for_label(_page, _label_text: str) -> dict[str, object]:
        last_attempt = attempts[-1] if attempts else ""
        if last_attempt == "Job Board":
            return {
                "fieldText": "How Did You Hear About Us?",
                "inputValue": "",
                "promptInstruction": "0 items selected",
                "selectedItems": [],
                "visibleOptions": ["TrueUp", "LinkedIn"],
                "highlightedOptions": [],
            }
        if last_attempt == "TrueUp":
            return {
                "fieldText": "How Did You Hear About Us?",
                "inputValue": "",
                "promptInstruction": "1 item selected, TrueUp",
                "selectedItems": ["TrueUp"],
                "visibleOptions": ["TrueUp", "LinkedIn"],
                "highlightedOptions": [],
            }
        return {
            "fieldText": "How Did You Hear About Us?",
            "inputValue": "",
            "promptInstruction": "0 items selected",
            "selectedItems": [],
            "visibleOptions": ["Job Board", "Adobe Source"],
            "highlightedOptions": [],
        }

    with (
        mock.patch.object(mod, "_fill_workday_source_prompt", return_value=None),
        mock.patch.object(mod, "_workday_source_search_candidates", side_effect=_source_candidates),
        mock.patch.object(mod, "_select_workday_prompt_option_via_input", side_effect=_record_input_attempt),
        mock.patch.object(mod, "_select_workday_prompt_option_via_locator", side_effect=_record_locator_attempt),
        mock.patch.object(mod, "_select_workday_prompt_option_for_label", return_value=None),
        mock.patch.object(mod, "_click_visible_workday_prompt_option", return_value=None),
        mock.patch.object(mod, "_select_workday_prompt_option_via_keyboard", return_value=None),
        mock.patch.object(mod, "_workday_prompt_state_for_label", side_effect=_state_for_label),
        mock.patch.object(mod, "_clear_mismatched_workday_prompt_selection", return_value=False),
        mock.patch.object(mod, "_workday_radio_question_labels", return_value=[]),
        mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=False),
    ):
        filled = mod._fill_my_information(page, profile, application_profile)

    assert any(entry["field_name"] == "source" and entry["value"] == "TrueUp" for entry in filled)
    assert attempts[:2] == ["Job Board", "Job Board"]
    assert "Adobe Source" not in attempts


def test_fill_my_information_skips_source_button_fallback_after_prompt_fill_succeeds():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    profile = SimpleNamespace(email="", phone="", location="")
    application_profile = SimpleNamespace(how_did_you_hear="")
    source_button = _DummyLocator([_DummyElement("How Did You Hear About Us")])
    page = mock.Mock()
    page.locator.return_value = _EmptyLocator()
    page.wait_for_timeout.return_value = None
    page.keyboard = mock.Mock()

    def _get_by_role(role: str, name: str):
        if role == "button" and name == "How Did You Hear About Us":
            return source_button
        return _EmptyLocator()

    page.get_by_role.side_effect = _get_by_role
    page.evaluate.return_value = None

    with (
        mock.patch.object(mod, "_fill_workday_source_prompt", return_value="LinkedIn"),
        mock.patch.object(mod, "_workday_radio_question_labels", return_value=[]),
        mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=False),
    ):
        filled = mod._fill_my_information(page, profile, application_profile)

    assert any(entry["field_name"] == "source" and entry["value"] == "LinkedIn" for entry in filled)
    evaluated_scripts = [str(call.args[0]) for call in page.evaluate.call_args_list if call.args]
    assert not any("const allFields = document.querySelectorAll" in script for script in evaluated_scripts)
    assert not any("const allOpts = document.querySelectorAll" in script for script in evaluated_scripts)


def test_fill_my_information_prefers_single_legal_name_fields_when_preferred_name_duplicates_exist():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="formField-legalFirstName">
      <label for="name--legalName--firstName">First Name</label>
      <input id="name--legalName--firstName" type="text" value="" />
    </div>
    <div role="group" aria-label="Preferred Name">
      <div data-automation-id="formField-preferredFirstName">
        <label for="name--preferredName--firstName">First Name</label>
        <input id="name--preferredName--firstName" type="text" value="" />
      </div>
    </div>
    <div data-automation-id="formField-legalLastName">
      <label for="name--legalName--lastName">Last Name</label>
      <input id="name--legalName--lastName" type="text" value="" />
    </div>
    <div role="group" aria-label="Preferred Name">
      <div data-automation-id="formField-preferredLastName">
        <label for="name--preferredName--lastName">Last Name</label>
        <input id="name--preferredName--lastName" type="text" value="" />
      </div>
    </div>
    """

    profile = SimpleNamespace(first_name="Jerrison", last_name="Li", email="", phone="")
    application_profile = SimpleNamespace(location="", how_did_you_hear="")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        with (
            mock.patch.object(mod, "_workday_radio_question_labels", return_value=[]),
            mock.patch.object(mod, "_check_workday_acknowledgment_checkbox", return_value=False),
        ):
            filled = mod._fill_my_information(page, profile, application_profile)

        assert page.locator("#name--legalName--firstName").input_value() == "Jerrison"
        assert page.locator("#name--preferredName--firstName").input_value() == ""
        assert page.locator("#name--legalName--lastName").input_value() == "Li"
        assert page.locator("#name--preferredName--lastName").input_value() == ""
        assert {item["field_name"] for item in filled} >= {"first_name", "last_name"}

        browser.close()


def test_check_workday_acknowledgment_checkbox_accepts_accommodations_acknowledgment():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    attempted_labels: list[str] = []

    def _fake_check(_page, label_text: str) -> bool:
        attempted_labels.append(label_text)
        return "accommodations information above" in label_text.casefold()

    with mock.patch.object(mod, "_check_workday_checkbox_for_label", side_effect=_fake_check):
        assert mod._check_workday_acknowledgment_checkbox(object()) is True

    assert any("accommodations information above" in label.casefold() for label in attempted_labels)


def test_check_workday_acknowledgment_checkbox_supports_unlabeled_checkbox_near_text_fragment():
    mod = load_module("autofill_workday_acknowledgment_unlabeled", "scripts/autofill_workday.py")
    html = """
    <div data-automation-id="acknowledgment-section">
      <div class="css-checkboxRow">
        <input id="acknowledgment" type="checkbox" />
        <span>
          I certify that my answers to all questions are true and correct without any consequential omissions of any kind whatsoever.
        </span>
      </div>
    </div>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert mod._check_workday_acknowledgment_checkbox(page) is True
        assert page.locator('[id="acknowledgment"]').is_checked() is True

        browser.close()


def test_run_workday_browser_captures_page_screenshots_after_fields_are_filled(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    (tmp_path / "master_resume.md").write_text("# resume", encoding="utf-8")
    (tmp_path / "application_profile.md").write_text("# profile", encoding="utf-8")
    out_dir = tmp_path / "role-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    pages_dir = submit_dir / "workday_autofill_pages"
    payload_path = submit_dir / "workday_autofill_payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "job_url": "https://example.test/job",
                "out_dir": str(out_dir),
                "candidate_email": "candidate@example.test",
                "company": "Example",
                "job_title": "Example Role",
                "artifacts": {
                    "page_screenshots_dir": str(pages_dir),
                    "pre_submit_screenshot": str(submit_dir / "workday_autofill_pre_submit.png"),
                    "submit_debug_screenshot": str(submit_dir / "workday_submit_debug.png"),
                    "submit_debug_html": str(submit_dir / "workday_submit_debug.html"),
                    "report_json": str(submit_dir / "workday_autofill_report.json"),
                    "report_markdown": str(submit_dir / "workday_autofill_report.md"),
                },
            }
        ),
        encoding="utf-8",
    )

    captured_states: list[tuple[str, str]] = []

    class _FakePage:
        def __init__(self) -> None:
            self.capture_state = "before_fill"

        def goto(self, *_args, **_kwargs) -> None:
            return None

        def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        def content(self) -> str:
            return "<html></html>"

        def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return "Standard Workday application flow"

        def locator(self, selector: str):
            if selector == "main":
                return _DummyLocator()
            return _DummyLocator()

        def get_by_role(self, role: str, name: str, exact: bool | None = None):
            del role, name, exact
            return _DummyLocator()

    class _FakeBrowser:
        def __init__(self, page: _FakePage) -> None:
            self.page = page

        def new_page(self, **_kwargs) -> _FakePage:
            return self.page

        def close(self) -> None:
            return None

    class _FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    fake_page = _FakePage()
    fake_browser = _FakeBrowser(fake_page)

    def _fake_fill_my_information(page, *_args, **_kwargs):
        page.capture_state = "after_fill"
        return [{"field_name": "source", "value": "Adobe.com", "source": "deterministic", "filled": True}]

    def _fake_capture(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
        del preferred_selectors
        captured_states.append((Path(path).name, page.capture_state))

    with (
        mock.patch("playwright.sync_api.sync_playwright", return_value=_FakePlaywrightContext()),
        mock.patch.object(mod, "launch_chromium_browser", return_value=fake_browser),
        mock.patch.object(mod, "_workday_credentials", return_value=("candidate@example.test", "secret")),
        mock.patch.object(mod, "load_meta", return_value={}),
        mock.patch.object(mod, "parse_master_resume", return_value=SimpleNamespace(email="candidate@example.test")),
        mock.patch.object(mod, "parse_application_profile", return_value=SimpleNamespace()),
        mock.patch.object(mod, "_clear_workday_failure_artifacts", return_value=None),
        mock.patch.object(mod, "_is_workday_already_applied_job_page", return_value=False),
        mock.patch.object(mod, "_handle_auth", return_value={"ok": True}),
        mock.patch.object(mod, "_is_application_page", return_value=True),
        mock.patch.object(mod, "_detect_current_page", side_effect=[mod.PAGE_MY_INFO, mod.PAGE_REVIEW]),
        mock.patch.object(mod, "_fill_my_information", side_effect=_fake_fill_my_information),
        mock.patch.object(mod, "_capture", side_effect=_fake_capture),
        mock.patch.object(mod, "_click_next_button", return_value=True),
        mock.patch.object(mod, "_write_workday_review_artifacts", return_value={}),
        mock.patch.object(mod, "PROJECT_ROOT", tmp_path),
        mock.patch.object(mod, "APPLICATION_PROFILE_PATH", tmp_path / "application_profile.md"),
        mock.patch.object(mod, "submit_viewport", return_value={"width": 1280, "height": 720}),
        mock.patch.object(mod, "submit_slow_mo_ms", return_value=0),
        mock.patch.object(mod, "submit_browser_profile_dir", return_value=tmp_path / "profile"),
    ):
        result = mod._run_workday_browser(payload_path, headless=True, submit=False)

    assert result == 0
    assert captured_states[0] == ("page_01_my_information.png", "after_fill")


def test_run_workday_browser_treats_public_profile_submit_boundary_as_review_in_draft_mode(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    (tmp_path / "master_resume.md").write_text("# resume", encoding="utf-8")
    (tmp_path / "application_profile.md").write_text("# profile", encoding="utf-8")
    out_dir = tmp_path / "role-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    pages_dir = submit_dir / "workday_autofill_pages"
    payload_path = submit_dir / "workday_autofill_payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "job_url": "https://example.test/job",
                "out_dir": str(out_dir),
                "candidate_email": "candidate@example.test",
                "company": "Example",
                "job_title": "Example Role",
                "artifacts": {
                    "page_screenshots_dir": str(pages_dir),
                    "pre_submit_screenshot": str(submit_dir / "workday_autofill_pre_submit.png"),
                    "submit_debug_screenshot": str(submit_dir / "workday_submit_debug.png"),
                    "submit_debug_html": str(submit_dir / "workday_submit_debug.html"),
                    "report_json": str(submit_dir / "workday_autofill_report.json"),
                    "report_markdown": str(submit_dir / "workday_autofill_report.md"),
                },
            }
        ),
        encoding="utf-8",
    )

    captured_paths: list[str] = []

    class _FakePublicProfilePage:
        def __init__(self) -> None:
            self.capture_state = "before_fill"
            self.url = "https://example.test/job/apply/applyManually"
            self.goto_calls: list[str] = []

        def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
            del wait_until, timeout
            self.goto_calls.append(url)
            self.url = url

        def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        def content(self) -> str:
            return "<html></html>"

        def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return (
                "Apply for Future Opportunities "
                "First Name Last Name Email Phone Number Resume/CV Upload Submit"
            )

        def locator(self, selector: str):
            if selector == "button[data-automation-id='submitButton']":
                return _DummyLocator([_DummyElement("Submit", visible=True)])
            if selector == "main":
                return _DummyLocator(
                    elements=[_DummyElement("main", visible=True)],
                    children={"h2, h3": _DummyLocator([_DummyElement("Apply for Future Opportunities")])},
                )
            return _DummyLocator()

        def get_by_role(self, role: str, name: str, exact: bool | None = None):
            del exact
            if role == "button" and name == "Submit":
                return _DummyLocator([_DummyElement("Submit", visible=True)])
            return _DummyLocator()

    class _FakeBrowser:
        def __init__(self, page: _FakePublicProfilePage) -> None:
            self.page = page

        def new_page(self, **_kwargs) -> _FakePublicProfilePage:
            return self.page

        def close(self) -> None:
            return None

    class _FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    fake_page = _FakePublicProfilePage()
    fake_browser = _FakeBrowser(fake_page)

    def _fake_fill_my_information(page, *_args, **_kwargs):
        page.capture_state = "after_fill"
        return [{"field_name": "first_name", "value": "Jerrison", "source": "master_resume.md", "filled": True}]

    def _fake_fill_my_experience(_page, _out_dir):
        return [{"field_name": "resume", "value": "resume.pdf", "source": "documents/", "filled": True}]

    def _fake_capture(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
        del page, preferred_selectors
        captured_paths.append(Path(path).name)

    with ExitStack() as stack:
        stack.enter_context(mock.patch("playwright.sync_api.sync_playwright", return_value=_FakePlaywrightContext()))
        stack.enter_context(mock.patch.object(mod, "launch_chromium_browser", return_value=fake_browser))
        stack.enter_context(mock.patch.object(mod, "_workday_credentials", return_value=("candidate@example.test", "secret")))
        stack.enter_context(mock.patch.object(mod, "load_meta", return_value={}))
        stack.enter_context(
            mock.patch.object(mod, "parse_master_resume", return_value=SimpleNamespace(email="candidate@example.test"))
        )
        stack.enter_context(mock.patch.object(mod, "parse_application_profile", return_value=SimpleNamespace()))
        stack.enter_context(mock.patch.object(mod, "_clear_workday_failure_artifacts", return_value=None))
        stack.enter_context(mock.patch.object(mod, "_is_workday_already_applied_job_page", return_value=False))
        stack.enter_context(mock.patch.object(mod, "_handle_auth", return_value={"ok": True}))
        stack.enter_context(mock.patch.object(mod, "_is_application_page", return_value=True))
        stack.enter_context(mock.patch.object(mod, "_detect_current_page", side_effect=lambda _page: mod.PAGE_MY_INFO))
        stack.enter_context(mock.patch.object(mod, "_fill_my_information", side_effect=_fake_fill_my_information))
        fill_experience = stack.enter_context(
            mock.patch.object(mod, "_fill_my_experience", side_effect=_fake_fill_my_experience)
        )
        stack.enter_context(mock.patch.object(mod, "_capture", side_effect=_fake_capture))
        stack.enter_context(mock.patch.object(mod, "_click_next_button", return_value=False))
        write_review = stack.enter_context(mock.patch.object(mod, "_write_workday_review_artifacts", return_value={}))
        stack.enter_context(mock.patch.object(mod, "_maybe_write_truthful_workday_stuck_result", return_value=False))
        stack.enter_context(mock.patch.object(mod, "PROJECT_ROOT", tmp_path))
        stack.enter_context(mock.patch.object(mod, "APPLICATION_PROFILE_PATH", tmp_path / "application_profile.md"))
        stack.enter_context(mock.patch.object(mod, "submit_viewport", return_value={"width": 1280, "height": 720}))
        stack.enter_context(mock.patch.object(mod, "submit_slow_mo_ms", return_value=0))
        stack.enter_context(mock.patch.object(mod, "submit_browser_profile_dir", return_value=tmp_path / "profile"))
        result = mod._run_workday_browser(payload_path, headless=True, submit=False)

    assert result == 0
    assert "page_01_my_information.png" in captured_paths
    fill_experience.assert_called_once_with(fake_page, out_dir)
    write_review.assert_called_once()


def test_run_workday_browser_captures_application_questions_screenshot_before_pending_user_input(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    (tmp_path / "master_resume.md").write_text("# resume", encoding="utf-8")
    (tmp_path / "application_profile.md").write_text("# profile", encoding="utf-8")
    out_dir = tmp_path / "role-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    pages_dir = submit_dir / "workday_autofill_pages"
    payload_path = submit_dir / "workday_autofill_payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "job_url": "https://example.test/job",
                "out_dir": str(out_dir),
                "candidate_email": "candidate@example.test",
                "company": "Example",
                "job_title": "Example Role",
                "artifacts": {
                    "page_screenshots_dir": str(pages_dir),
                    "pre_submit_screenshot": str(submit_dir / "workday_autofill_pre_submit.png"),
                    "submit_debug_screenshot": str(submit_dir / "workday_submit_debug.png"),
                    "submit_debug_html": str(submit_dir / "workday_submit_debug.html"),
                    "report_json": str(submit_dir / "workday_autofill_report.json"),
                    "report_markdown": str(submit_dir / "workday_autofill_report.md"),
                },
            }
        ),
        encoding="utf-8",
    )

    captured_paths: list[str] = []
    pending_path = submit_dir / "pending_user_input.json"
    blocker = {
        "field_name": "salary_expectation",
        "label": "Salary expectation",
        "kind": "select",
        "required": True,
        "source": "application_profile.md",
        "status": "planned",
        "blocker_kind": "generated_answer",
        "blocks_draft_completion": True,
        "reason": "Requires explicit user input.",
    }

    class _FakePage:
        def goto(self, *_args, **_kwargs) -> None:
            return None

        def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        def content(self) -> str:
            return "<html></html>"

    class _FakeBrowser:
        def __init__(self, page: _FakePage) -> None:
            self.page = page

        def new_page(self, **_kwargs) -> _FakePage:
            return self.page

        def close(self) -> None:
            return None

    class _FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    fake_page = _FakePage()
    fake_browser = _FakeBrowser(fake_page)

    def _fake_capture(_page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
        del preferred_selectors
        captured_paths.append(Path(path).name)

    with (
        mock.patch("playwright.sync_api.sync_playwright", return_value=_FakePlaywrightContext()),
        mock.patch.object(mod, "launch_chromium_browser", return_value=fake_browser),
        mock.patch.object(mod, "_workday_credentials", return_value=("candidate@example.test", "secret")),
        mock.patch.object(mod, "load_meta", return_value={}),
        mock.patch.object(mod, "parse_master_resume", return_value=SimpleNamespace(email="candidate@example.test")),
        mock.patch.object(mod, "parse_application_profile", return_value=SimpleNamespace()),
        mock.patch.object(mod, "_clear_workday_failure_artifacts", return_value=None),
        mock.patch.object(mod, "_is_workday_already_applied_job_page", return_value=False),
        mock.patch.object(mod, "_handle_auth", return_value={"ok": True}),
        mock.patch.object(mod, "_is_application_page", return_value=True),
        mock.patch.object(mod, "_detect_current_page", side_effect=[mod.PAGE_APPLICATION_QUESTIONS]),
        mock.patch.object(
            mod,
            "_fill_application_questions",
            side_effect=mod.GeneratedAnswerBlockersError([blocker]),
        ),
        mock.patch.object(mod, "_capture", side_effect=_fake_capture),
        mock.patch.object(
            mod,
            "_write_workday_pending_user_input_for_generated_answer_blockers",
            return_value=pending_path,
        ),
        mock.patch.object(mod, "PROJECT_ROOT", tmp_path),
        mock.patch.object(mod, "APPLICATION_PROFILE_PATH", tmp_path / "application_profile.md"),
        mock.patch.object(mod, "submit_viewport", return_value={"width": 1280, "height": 720}),
        mock.patch.object(mod, "submit_slow_mo_ms", return_value=0),
        mock.patch.object(mod, "submit_browser_profile_dir", return_value=tmp_path / "profile"),
    ):
        result = mod._run_workday_browser(payload_path, headless=True, submit=False)

    assert result == 0
    assert "page_01_application_questions.png" in captured_paths


def test_maybe_write_truthful_workday_stuck_result_reclassifies_auth_gate(tmp_path):
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")
    payload = {
        "job_url": "https://example.test/job",
        "company": "Example",
        "job_title": "Example Role",
        "candidate_email": "candidate@example.test",
        "artifacts": {
            "submit_debug_screenshot": str(tmp_path / "workday_submit_debug.png"),
        },
    }
    markers = {
        "page_url": "https://example.test/login",
        "page_text_excerpt": "Sign In to continue",
        "heading_text": "Sign In",
        "alert_text": "",
        "visible_actions": ["Sign In", "Create Account"],
        "auth_state": "sign_in_gate",
    }

    with (
        mock.patch.object(mod, "_is_application_page", return_value=False),
        mock.patch.object(mod, "_extract_workday_auth_markers", return_value=markers),
        mock.patch.object(mod, "_capture", return_value=None),
        mock.patch.object(mod, "_write_workday_auth_result") as write_auth_result,
    ):
        handled = mod._maybe_write_truthful_workday_stuck_result(
            object(),
            tmp_path,
            payload,
            current_page=mod.PAGE_SELF_IDENTIFY,
        )

    assert handled is True
    write_auth_result.assert_called_once()


def test_compose_workday_pre_submit_screenshot_concatenates_page_sequence():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    from PIL import Image

    with mock.patch.object(mod, "PROJECT_ROOT", Path.cwd()):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pages_dir = tmp_path / "pages"
            pages_dir.mkdir()
            review_path = pages_dir / "page_03_review.png"
            Image.new("RGB", (12, 8), "red").save(pages_dir / "page_01_my_information.png")
            Image.new("RGB", (10, 6), "green").save(pages_dir / "page_02_application_questions.png")
            Image.new("RGB", (12, 10), "blue").save(review_path)

            output_path = tmp_path / "workday_autofill_pre_submit.png"
            mod._compose_workday_pre_submit_screenshot(pages_dir, output_path)

            combined = Image.open(output_path)
            assert combined.size == (12, 24)
            assert combined.getpixel((0, 0)) == (255, 0, 0)
            assert combined.getpixel((1, 10)) == (0, 128, 0)
            assert combined.getpixel((1, 20)) == (0, 0, 255)


def test_write_workday_review_artifacts_persists_filled_fields_to_report():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    from PIL import Image

    with mock.patch.object(mod, "PROJECT_ROOT", Path.cwd()):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pages_dir = tmp_path / "pages"
            pages_dir.mkdir()
            Image.new("RGB", (12, 8), "red").save(pages_dir / "page_01_my_information.png")
            Image.new("RGB", (12, 10), "blue").save(pages_dir / "page_02_review.png")

            payload = {
                "job_title": "Senior Product Manager",
                "company": "Turo",
                "job_url": "https://turo.wd5.myworkdayjobs.com/en-US/jobs/job/example",
                "artifacts": {
                    "report_json": str(tmp_path / "workday_autofill_report.json"),
                    "report_markdown": str(tmp_path / "workday_autofill_report.md"),
                    "pre_submit_screenshot": str(tmp_path / "workday_autofill_pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }

            report_payload = mod._write_workday_review_artifacts(
                payload,
                filled_steps=[
                    {
                        "field_name": "phone",
                        "label": "Phone",
                        "kind": "text",
                        "value": "510-613-5192",
                        "source": "master_resume.md",
                        "required": True,
                        "filled": True,
                    }
                ],
                page_screenshots_dir=pages_dir,
            )
            saved = json.loads((tmp_path / "workday_autofill_report.json").read_text(encoding="utf-8"))
            assert Path(payload["artifacts"]["pre_submit_screenshot"]).exists()
            assert len(saved["fields"]) == 1
            assert saved["fields"][0]["field_name"] == "phone"
            assert report_payload["fields"][0]["field_name"] == "phone"


def test_write_workday_review_artifacts_dedupes_near_duplicate_page_screenshots():
    mod = load_module("autofill_workday", "scripts/autofill_workday.py")

    from PIL import Image, ImageDraw

    with mock.patch.object(mod, "PROJECT_ROOT", Path.cwd()):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pages_dir = tmp_path / "pages"
            pages_dir.mkdir()

            page_one = pages_dir / "page_01_self_identify.png"
            page_two = pages_dir / "page_02_self_identify.png"
            page_three = pages_dir / "page_03_review.png"

            base = Image.new("RGB", (320, 200), "white")
            draw = ImageDraw.Draw(base)
            draw.rectangle((16, 16, 304, 56), outline="black", width=2)
            draw.text((24, 24), "Self Identify", fill="black")
            draw.text((24, 92), "No, I do not have a disability", fill="black")
            base.save(page_one)

            near_duplicate = base.copy()
            near_duplicate.putpixel((310, 190), (240, 240, 240))
            near_duplicate.save(page_two)

            review = Image.new("RGB", (320, 180), "white")
            draw = ImageDraw.Draw(review)
            draw.rectangle((16, 16, 304, 56), outline="black", width=2)
            draw.text((24, 24), "Review", fill="black")
            draw.text((24, 92), "Submit", fill="black")
            review.save(page_three)

            payload = {
                "job_title": "Senior Product Manager",
                "company": "Adobe",
                "job_url": "https://adobe.wd5.myworkdayjobs.com/example",
                "artifacts": {
                    "report_json": str(tmp_path / "workday_autofill_report.json"),
                    "report_markdown": str(tmp_path / "workday_autofill_report.md"),
                    "pre_submit_screenshot": str(tmp_path / "workday_autofill_pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }

            report_payload = mod._write_workday_review_artifacts(
                payload,
                filled_steps=[],
                page_screenshots_dir=pages_dir,
            )

            saved = json.loads((tmp_path / "workday_autofill_report.json").read_text(encoding="utf-8"))
            assert report_payload["page_screenshots"] == [str(page_one), str(page_three)]
            assert saved["page_screenshots"] == [str(page_one), str(page_three)]
            assert page_one.exists() is True
            assert page_two.exists() is False
            assert page_three.exists() is True
            assert Image.open(payload["artifacts"]["pre_submit_screenshot"]).size == (320, 380)
