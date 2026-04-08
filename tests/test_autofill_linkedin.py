# tests/test_autofill_linkedin.py
"""Tests for LinkedIn Easy Apply board."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LinkedInUrlDetectionTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("job_board_urls", "scripts/job_board_urls.py")

    def test_looks_like_linkedin_easy_apply_url_positive(self):
        assert self.mod.looks_like_linkedin_easy_apply_url("https://www.linkedin.com/jobs/view/1234567890/")

    def test_looks_like_linkedin_easy_apply_url_with_query_params(self):
        assert self.mod.looks_like_linkedin_easy_apply_url(
            "https://www.linkedin.com/jobs/view/1234567890/?currentJobId=123&refId=abc"
        )

    def test_looks_like_linkedin_easy_apply_url_negative_non_job(self):
        assert not self.mod.looks_like_linkedin_easy_apply_url("https://www.linkedin.com/in/someone/")

    def test_looks_like_linkedin_easy_apply_url_negative_other_site(self):
        assert not self.mod.looks_like_linkedin_easy_apply_url("https://lever.co/jobs/1234")

    def test_canonical_linkedin_job_url_strips_query_params(self):
        result = self.mod.canonical_linkedin_job_url(
            "https://www.linkedin.com/jobs/view/1234567890/?currentJobId=123&refId=abc&trk=foo"
        )
        assert result == "https://www.linkedin.com/jobs/view/1234567890/"

    def test_canonical_linkedin_job_url_adds_trailing_slash(self):
        result = self.mod.canonical_linkedin_job_url("https://www.linkedin.com/jobs/view/1234567890")
        assert result == "https://www.linkedin.com/jobs/view/1234567890/"

    def test_canonical_linkedin_job_url_preserves_already_clean(self):
        url = "https://www.linkedin.com/jobs/view/1234567890/"
        assert self.mod.canonical_linkedin_job_url(url) == url


class LinkedInUrlResolverTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("url_resolver", "scripts/url_resolver.py")

    def test_is_known_board_url_recognizes_linkedin_jobs_view(self):
        assert self.mod._is_known_board_url("https://www.linkedin.com/jobs/view/1234567890/")

    def test_is_known_board_url_rejects_linkedin_profile(self):
        assert not self.mod._is_known_board_url("https://www.linkedin.com/in/someone/")


class LinkedInBoardRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("submit_application", "scripts/submit_application.py")

    def test_board_for_url_detects_linkedin(self):
        result = self.mod._board_for_url(
            "https://www.linkedin.com/jobs/view/1234567890/",
            extraction_method="",
            application_method="",
        )
        assert result == "linkedin"

    def test_script_for_board_returns_linkedin_script(self):
        path = self.mod._script_for_board("linkedin")
        assert path.name == "autofill_linkedin.py"
        assert path.parent.name == "scripts"


class LinkedInPayloadTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def test_build_payload_returns_correct_board(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            # Write minimal meta
            (out_dir / ".pipeline_meta.json").write_text(
                '{"jd_source": "https://www.linkedin.com/jobs/view/123/", "role": "PM", "company": "Acme"}'
            )
            payload = self.mod._build_payload(out_dir)
            assert payload["board"] == "linkedin"
            assert payload["job_url"] == "https://www.linkedin.com/jobs/view/123/"
            assert payload["company"] == "Acme"
            assert payload["steps"] == []
            assert payload["fields"] == []
            # Verify artifacts dict has the keys that write_report expects
            artifacts = payload["artifacts"]
            assert "report_markdown" in artifacts, "artifacts must have 'report_markdown' for write_report"
            assert "report_json" in artifacts
            assert "pre_submit_screenshot" in artifacts
            assert "page_screenshots_dir" in artifacts

    def test_build_payload_finds_resume_pdf_in_submit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')
            # Create a fake resume PDF
            (submit_dir / "resume.pdf").write_bytes(b"%PDF-fake")
            payload = self.mod._build_payload(out_dir)
            assert payload["resume_path"] is not None
            assert "resume.pdf" in payload["resume_path"]

    def test_build_payload_finds_resume_pdf_in_documents(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "content").mkdir()
            docs_dir = out_dir / "documents"
            docs_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')
            (docs_dir / "Jerrison Li Resume - Acme.pdf").write_bytes(b"%PDF-fake")
            payload = self.mod._build_payload(out_dir)
            assert payload["resume_path"] is not None
            assert "Resume" in payload["resume_path"]

    def test_build_payload_finds_cover_letter_in_documents(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "content").mkdir()
            docs_dir = out_dir / "documents"
            docs_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')
            (docs_dir / "Jerrison Li Resume - Acme.pdf").write_bytes(b"%PDF-fake")
            (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_bytes(b"%PDF-fake")
            payload = self.mod._build_payload(out_dir)
            assert payload["cover_letter_path"] is not None
            assert "Cover Letter" in payload["cover_letter_path"]

    def test_build_payload_prefers_company_named_resume_when_multiple_files_exist(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "content").mkdir()
            docs_dir = out_dir / "documents"
            docs_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                '{"jd_url": "https://linkedin.com/jobs/view/1/", "company_proper": "Asurion"}'
            )
            (docs_dir / "Jerrison Li Resume - Linkedin.pdf").write_bytes(b"%PDF-linkedin")
            (docs_dir / "Jerrison Li Resume - Asurion.pdf").write_bytes(b"%PDF-asurion")
            payload = self.mod._build_payload(out_dir)
            assert payload["resume_path"] is not None
            assert payload["resume_path"].endswith("Jerrison Li Resume - Asurion.pdf")

    def test_build_payload_prefers_canonical_cover_letter_when_stale_variant_exists(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "content").mkdir()
            docs_dir = out_dir / "documents"
            docs_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                '{"jd_url": "https://linkedin.com/jobs/view/1/", "company_proper": "Cresta"}'
            )
            (docs_dir / "Jerrison Li Resume - Cresta.pdf").write_bytes(b"%PDF-resume")
            (docs_dir / "Jerrison Li Cover Letter - Cresta..pdf").write_bytes(b"%PDF-stale")
            (docs_dir / "Jerrison Li Cover Letter - Cresta.pdf").write_bytes(b"%PDF-canonical")

            payload = self.mod._build_payload(out_dir)

            assert payload["cover_letter_path"] is not None
            assert payload["cover_letter_path"].endswith("Jerrison Li Cover Letter - Cresta.pdf")

    def test_build_payload_exposes_candidate_profile_fields(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')
            (content_dir / "master_resume.md").write_text(
                (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
            )
            (content_dir / "application_profile.md").write_text(
                (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
            )

            payload = self.mod._build_payload(out_dir)

            assert payload["candidate_location"] == "San Francisco, CA"
            assert payload["candidate_linkedin"] == "https://www.linkedin.com/in/jerrison/"
            assert payload["candidate_website"] == "https://jerrisonli.com"

    def test_build_payload_falls_back_to_repo_source_materials_when_output_copies_missing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "content").mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')

            payload = self.mod._build_payload(out_dir)

            assert payload["candidate_name"]
            assert payload["candidate_email"]
            assert payload["candidate_phone"]
            assert payload["candidate_location"] == "San Francisco, CA"
            assert payload["candidate_linkedin"] == "https://www.linkedin.com/in/jerrison/"
            assert payload["candidate_website"] == "https://jerrisonli.com"


class _FakeLinkedInLocator:
    def __init__(
        self,
        *,
        visible: bool = False,
        on_click=None,
        click_exc: Exception | None = None,
        dispatch_exc: Exception | None = None,
        count_value: int = 1,
        bounding_box_value: dict[str, float] | None = None,
        inner_text_value: str = "",
        aria_label: str | None = None,
        items: list["_FakeLinkedInLocator"] | None = None,
    ):
        self.visible = visible
        self.on_click = on_click
        self.click_exc = click_exc
        self.dispatch_exc = dispatch_exc
        self.click_count = 0
        self.dispatch_count = 0
        self.scroll_count = 0
        self.count_value = count_value
        self.bounding_box_value = bounding_box_value
        self.inner_text_value = inner_text_value
        self.aria_label = aria_label
        self.items = items
        self.screenshot_calls = []

    @property
    def first(self):
        return self.nth(0)

    def count(self):
        if self.items is not None:
            return len(self.items)
        return self.count_value

    def nth(self, index: int):
        if self.items is not None:
            return self.items[index]
        return self

    def is_visible(self, timeout=0):
        return self.visible

    def scroll_into_view_if_needed(self, timeout=0):
        self.scroll_count += 1

    def click(self, force=False, timeout=None, no_wait_after=None):
        del timeout, no_wait_after
        self.click_count += 1
        if self.click_exc is not None:
            raise self.click_exc
        if self.on_click is not None:
            self.on_click()

    def dispatch_event(self, event: str):
        self.dispatch_count += 1
        if self.dispatch_exc is not None:
            raise self.dispatch_exc
        if event == "click" and self.on_click is not None:
            self.on_click()

    def inner_text(self):
        return self.inner_text_value

    def get_attribute(self, name: str):
        if name == "aria-label":
            return self.aria_label
        return None

    def bounding_box(self):
        return self.bounding_box_value

    def screenshot(self, *, path, type="png"):
        self.screenshot_calls.append({"path": path, "type": type})


class _FakeLinkedInTextarea:
    def __init__(self, value: str):
        self._value = value
        self.cleared = False

    def input_value(self):
        return self._value

    def clear(self):
        self.cleared = True
        self._value = ""


class _FakeLinkedInTextareaClearFails(_FakeLinkedInTextarea):
    def clear(self):
        raise RuntimeError("clear failed")


class _FakeLinkedInInput(_FakeLinkedInTextarea):
    def __init__(self, value: str, *, input_type: str = "text"):
        super().__init__(value)
        self.input_type = input_type

    def get_attribute(self, name: str):
        if name == "type":
            return self.input_type
        return None


class _FakeFieldsetLegendLocator:
    def __init__(self, text: str):
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.text else 0

    def inner_text(self):
        return self.text


class _FakeFieldsetLabelLocator(_FakeFieldsetLegendLocator):
    pass


class _FakeFieldsetInputList:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def all(self):
        return list(self.items)

    def nth(self, index: int):
        return self.items[index]


class _FakeFieldsetCheckbox:
    def __init__(self, input_id: str, *, value: str = "", checked: bool = False, aria_label: str = ""):
        self.input_id = input_id
        self.value = value
        self.checked = checked
        self.aria_label = aria_label
        self.click_calls = 0

    def get_attribute(self, name: str):
        if name == "id":
            return self.input_id
        if name == "value":
            return self.value
        if name == "aria-label":
            return self.aria_label
        return None

    def is_checked(self):
        return self.checked

    def click(self, force: bool = False):
        del force
        self.checked = not self.checked
        self.click_calls += 1


class _FakeLinkedInFieldset:
    def __init__(self, legend_text: str, checkbox_specs: list[tuple[_FakeFieldsetCheckbox, str]]):
        self.legend_locator = _FakeFieldsetLegendLocator(legend_text)
        self.checkbox_inputs = _FakeFieldsetInputList([cb for cb, _ in checkbox_specs])
        self.checkbox_labels = {
            checkbox.input_id: _FakeFieldsetLabelLocator(label_text) for checkbox, label_text in checkbox_specs
        }

    def locator(self, selector: str):
        if selector == "legend, span.fb-dash-form-element__label":
            return self.legend_locator
        if selector == 'input[type="checkbox"]':
            return self.checkbox_inputs
        if selector.startswith('label[for="') and selector.endswith('"]'):
            input_id = selector[len('label[for="') : -2]
            return self.checkbox_labels.get(input_id, _FakeFieldsetLabelLocator(""))
        raise AssertionError(f"Unexpected selector: {selector}")


class _FakeLinkedInPage:
    def __init__(
        self,
        *,
        modal: _FakeLinkedInLocator,
        easy_apply_button: _FakeLinkedInLocator,
        external_apply_button: _FakeLinkedInLocator | None = None,
        selector_locators: dict[str, _FakeLinkedInLocator] | None = None,
        viewport_size: dict[str, int] | None = None,
        clip_error: Exception | None = None,
    ):
        self.modal = modal
        self.easy_apply_button = easy_apply_button
        self.external_apply_button = external_apply_button or _FakeLinkedInLocator(visible=False, count_value=0)
        self.selector_locators = selector_locators or {}
        self.viewport_size = viewport_size or {"width": 1360, "height": 900}
        self.clip_error = clip_error
        self.wait_calls = []
        self.screenshot_calls = []

    def locator(self, selector: str):
        if "jobs-easy-apply-modal" in selector:
            return self.modal
        if selector in self.selector_locators:
            return self.selector_locators[selector]
        if "Apply to this job" in selector or ':has-text("Apply")' in selector:
            return self.external_apply_button
        if "Easy Apply" in selector:
            return self.easy_apply_button
        raise AssertionError(f"Unexpected selector: {selector}")

    def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    def screenshot(self, **kwargs):
        if "clip" in kwargs and self.clip_error is not None:
            raise self.clip_error
        self.screenshot_calls.append(dict(kwargs))


class LinkedInUncheckFollowTests(unittest.TestCase):
    def test_uncheck_follow_company_function_exists(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        assert callable(mod._uncheck_follow_company)

    def test_uncheck_follow_company_handles_follow_company_label_variant(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class _Checkbox:
            def __init__(self, checked: bool) -> None:
                self.checked = checked
                self.click_calls = 0
                self.uncheck_calls = 0

            def is_checked(self) -> bool:
                return self.checked

            def click(self, force: bool = False) -> None:
                del force
                self.checked = False
                self.click_calls += 1

            def uncheck(self, force: bool = False, timeout: int = 0) -> None:
                del force, timeout
                self.checked = False
                self.uncheck_calls += 1

            def get_attribute(self, name: str):
                return None

        class _CheckboxLocator:
            def __init__(self, items):
                self.items = list(items)

            def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

            @property
            def first(self):
                return self.nth(0)

        class _Modal:
            def __init__(self, selector_locators):
                self.selector_locators = selector_locators

            def locator(self, selector: str):
                return self.selector_locators.get(selector, _CheckboxLocator([]))

        checkbox = _Checkbox(checked=True)
        modal = _Modal(
            {
                'label:has-text("Follow company") input[type="checkbox"]': _CheckboxLocator([checkbox]),
            }
        )

        steps = mod._uncheck_follow_company(modal)

        assert checkbox.uncheck_calls == 1
        assert checkbox.is_checked() is False
        assert steps == [
            {
                "field_name": "follow_company_opt_in",
                "label": "Follow company",
                "kind": "checkbox",
                "value": False,
                "source": "linkedin_opt_out_policy",
                "filled": True,
                "required": False,
            }
        ]

    def test_uncheck_follow_company_falls_back_to_label_for_hidden_checkbox(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class _Checkbox:
            def __init__(self) -> None:
                self.checked = True
                self.click_calls = 0
                self.label_click_calls = 0

            def is_checked(self) -> bool:
                return self.checked

            def uncheck(self, force: bool = False, timeout: int = 0) -> None:
                del force, timeout
                raise RuntimeError("hidden input")

            def click(self, force: bool = False) -> None:
                del force
                raise RuntimeError("hidden input")

            def get_attribute(self, name: str):
                if name == "id":
                    return "follow-company-checkbox"
                return None

        class _CheckboxLocator:
            def __init__(self, items):
                self.items = list(items)

            def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

            @property
            def first(self):
                return self.nth(0)

        class _Label:
            def __init__(self, checkbox: _Checkbox):
                self.checkbox = checkbox
                self.click_calls = 0

            @property
            def first(self):
                return self

            def count(self) -> int:
                return 1

            def click(self, force: bool = False) -> None:
                del force
                self.checkbox.checked = False
                self.click_calls += 1

            def inner_text(self) -> str:
                return "Follow Greylock Partners to stay up to date with their page."

        class _Modal:
            def __init__(self, checkbox: _Checkbox, label: _Label):
                self.checkbox = checkbox
                self.label = label

            def locator(self, selector: str):
                if selector == 'input[type="checkbox"][id*="follow"]':
                    return _CheckboxLocator([self.checkbox])
                if selector == 'label[for="follow-company-checkbox"]':
                    return self.label
                return _CheckboxLocator([])

        checkbox = _Checkbox()
        label = _Label(checkbox)
        modal = _Modal(checkbox, label)

        steps = mod._uncheck_follow_company(modal)

        assert label.click_calls == 1
        assert checkbox.is_checked() is False
        assert steps[0]["label"] == "Follow Greylock Partners to stay up to date with their page."

    def test_wizard_flow_unchecks_follow_company_on_review_step(self):
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class _ReviewModal:
            def __init__(self):
                self.submit_button = _FakeLinkedInLocator(visible=True, count_value=1)

            def is_visible(self, timeout=0):
                del timeout
                return True

            def inner_text(self):
                return "Review your application"

            def locator(self, selector: str):
                if selector == 'button:has-text("Submit application")':
                    return self.submit_button
                return _FakeLinkedInLocator(visible=False, count_value=0)

        class _ReviewPage:
            def __init__(self):
                self.url = "https://www.linkedin.com/jobs/view/123/"

            def goto(self, url: str, wait_until=None, timeout=None):
                del wait_until, timeout
                self.url = url

            def wait_for_timeout(self, ms: int):
                del ms

            def inner_text(self, selector: str):
                assert selector == "body"
                return "LinkedIn job page"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "job_url": "https://www.linkedin.com/jobs/view/123/",
                "artifacts": {
                    "pre_submit_screenshot": str(tmp_path / "linkedin_autofill_pre_submit.png"),
                },
                "steps": [],
                "unknown_questions": [],
            }
            page = _ReviewPage()
            modal = _ReviewModal()
            uncheck_step = {
                "field_name": "follow_company_opt_in",
                "label": "Follow Greylock Partners to stay up to date with their page.",
                "kind": "checkbox",
                "value": False,
                "source": "linkedin_opt_out_policy",
                "filled": True,
                "required": False,
            }

            with (
                mock.patch.object(mod, "_clear_current_attempt_linkedin_artifacts"),
                mock.patch.object(mod, "_easy_apply_button", return_value=_FakeLinkedInLocator(visible=True)),
                mock.patch.object(mod, "_easy_apply_modal", return_value=modal),
                mock.patch.object(mod, "_dismiss_discard_dialog"),
                mock.patch.object(mod, "_capture_linkedin_surface_screenshot"),
                mock.patch.object(
                    mod, "_locator_is_visible", side_effect=lambda loc, timeout=0: loc.is_visible(timeout)
                ),
                mock.patch.object(mod, "_uncheck_follow_company", return_value=[uncheck_step]) as uncheck_follow,
                mock.patch("autofill_common.write_report"),
            ):
                result = mod._wizard_flow(
                    page,
                    payload,
                    tmp_path,
                    submit=False,
                    pages_dir=tmp_path / "pages",
                )

            assert result == 0
            assert payload["steps"] == [uncheck_step]
            uncheck_follow.assert_called_once_with(modal)

    def test_wizard_flow_fills_single_step_submit_modal_before_breaking(self):
        import tempfile

        mod = load_module("autofill_linkedin_single_step_submit", "scripts/autofill_linkedin.py")

        class _SingleStepSubmitModal:
            def __init__(self):
                self.submit_button = _FakeLinkedInLocator(visible=True, count_value=1)
                self.next_button = _FakeLinkedInLocator(visible=False, count_value=0)

            def is_visible(self, timeout=0):
                del timeout
                return True

            def inner_text(self):
                return "Apply to Matterport Resume Upload resume"

            def locator(self, selector: str):
                if selector == 'button:has-text("Submit application")':
                    return self.submit_button
                if selector.startswith('button[aria-label="Continue to next step"]'):
                    return self.next_button
                return _FakeLinkedInLocator(visible=False, count_value=0)

        class _SingleStepSubmitPage:
            def __init__(self):
                self.url = "https://www.linkedin.com/jobs/view/123/"

            def goto(self, url: str, wait_until=None, timeout=None):
                del wait_until, timeout
                self.url = url

            def wait_for_timeout(self, ms: int):
                del ms

            def inner_text(self, selector: str):
                assert selector == "body"
                return "LinkedIn job page"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "job_url": "https://www.linkedin.com/jobs/view/123/",
                "artifacts": {
                    "pre_submit_screenshot": str(tmp_path / "linkedin_autofill_pre_submit.png"),
                },
                "steps": [],
                "unknown_questions": [],
            }
            page = _SingleStepSubmitPage()
            modal = _SingleStepSubmitModal()
            resume_step = {
                "field_name": "resume",
                "label": "upload resume",
                "kind": "file",
                "value": "/tmp/Jerrison Li Resume - Matterport.pdf",
                "source": "generated_resume",
                "filled": True,
                "required": True,
            }

            with (
                mock.patch.object(mod, "_clear_current_attempt_linkedin_artifacts"),
                mock.patch.object(mod, "_easy_apply_button", return_value=_FakeLinkedInLocator(visible=True)),
                mock.patch.object(mod, "_easy_apply_modal", return_value=modal),
                mock.patch.object(mod, "_dismiss_discard_dialog"),
                mock.patch.object(mod, "_capture_linkedin_surface_screenshot"),
                mock.patch.object(
                    mod, "_locator_is_visible", side_effect=lambda loc, timeout=0: loc.is_visible(timeout)
                ),
                mock.patch.object(mod, "_fill_wizard_step", return_value=[resume_step]) as fill_step,
                mock.patch.object(mod, "_uncheck_follow_company", return_value=[]),
                mock.patch("autofill_common.write_report"),
            ):
                result = mod._wizard_flow(
                    page,
                    payload,
                    tmp_path,
                    submit=False,
                    pages_dir=tmp_path / "pages",
                )

            assert result == 0
            fill_step.assert_called_once()
            call_args, call_kwargs = fill_step.call_args
            assert call_args[:4] == (page, modal, payload, tmp_path)
            assert call_kwargs["resume_runtime"] == {
                "status": "",
                "message": "",
                "visible_upload_path_seen": False,
                "observed_selection_labels": [],
            }
            assert payload["steps"] == [resume_step]

    def test_click_next_button_with_fallback_dispatches_click_after_timeout(self):
        mod = load_module("autofill_linkedin_next_fallback", "scripts/autofill_linkedin.py")
        next_btn = _FakeLinkedInLocator(
            visible=True,
            click_exc=RuntimeError("click timed out"),
        )

        mod._click_next_button_with_fallback(next_btn, step_num=5)

        assert next_btn.click_count == 1
        assert next_btn.dispatch_count == 1

    def test_click_next_button_with_fallback_raises_original_error_when_dispatch_fails(self):
        mod = load_module("autofill_linkedin_next_fallback_error", "scripts/autofill_linkedin.py")
        click_error = RuntimeError("click timed out")
        next_btn = _FakeLinkedInLocator(
            visible=True,
            click_exc=click_error,
            dispatch_exc=RuntimeError("dispatch failed"),
        )

        with self.assertRaises(RuntimeError) as excinfo:
            mod._click_next_button_with_fallback(next_btn, step_num=5)

        assert excinfo.exception is click_error
        assert next_btn.click_count == 1
        assert next_btn.dispatch_count == 1

    def test_answer_for_select_returns_linkedin_for_how_hear(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "How did you hear about us?",
            ["LinkedIn", "Google", "Referral", "Other"],
            {},
            Path("/tmp"),
        )
        assert result == "LinkedIn"


class LinkedInTextareaTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def test_normalize_text_field_answer_truncates_headline_to_single_line_word_boundary(self):
        answer = (
            "Lead PM building AI and ML products from 0-to-1 to scale, excited to turn spatial computing "
            "and visual AI into intuitive IKEA home design experiences"
        )

        normalized = self.mod._normalize_text_field_answer("Headline", answer, input_type="text")

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertLessEqual(len(normalized), 100)
        self.assertNotIn("\n", normalized)
        self.assertTrue(normalized.startswith("Lead PM building AI and ML products"))

    def test_fill_textarea_field_overrides_prefilled_cover_letter_with_generated_text(self):
        import tempfile

        textarea = _FakeLinkedInTextarea(
            "I am someone who is consistently growing themselves and who takes the time to continue learning."
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            cover_letter_text = "Dear Hiring Team,\n\nThis is the tailored cover letter."
            (content_dir / "cover_letter_text.txt").write_text(cover_letter_text, encoding="utf-8")

            fake_browser_runtime = types.SimpleNamespace(
                human_fill=lambda locator, value: setattr(locator, "_value", value)
            )

            with mock.patch.object(self.mod, "_get_field_label", return_value="Cover letter"):
                with mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}):
                    self.mod._fill_textarea_field(
                        textarea,
                        modal=object(),
                        payload={},
                        out_dir=out_dir,
                        filled_steps=filled_steps,
                        unknown_questions=unknown_questions,
                    )

        self.assertEqual(len(filled_steps), 1)
        self.assertEqual(filled_steps[0]["source"], "cover_letter_text.txt")
        self.assertEqual(filled_steps[0]["value"], "Dear Hiring Team,\n\nThis is the tailored cover letter.")
        self.assertEqual(textarea._value, "Dear Hiring Team,\n\nThis is the tailored cover letter.")
        self.assertTrue(textarea.cleared)
        self.assertEqual(unknown_questions, [])

    def test_fill_textarea_field_tolerates_clear_failure_when_generating_answer(self):
        import tempfile

        textarea = _FakeLinkedInTextareaClearFails("")
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        fake_browser_runtime = types.SimpleNamespace(
            human_fill=lambda locator, value: setattr(locator, "_value", value)
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            with (
                mock.patch.object(self.mod, "_get_field_label", return_value="Why this role"),
                mock.patch("question_classifier.classify_question", return_value=None),
                mock.patch(
                    "application_submit_common.generate_application_answers",
                    return_value={"why_this_role": "I want to build agentic product systems at scale."},
                ),
                mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}),
            ):
                self.mod._fill_textarea_field(
                    textarea,
                    modal=object(),
                    payload={},
                    out_dir=out_dir,
                    filled_steps=filled_steps,
                    unknown_questions=unknown_questions,
                )

        self.assertEqual(textarea._value, "I want to build agentic product systems at scale.")
        self.assertEqual(filled_steps[0]["source"], "generated_application_answer")
        self.assertEqual(unknown_questions, [])

    def test_fill_text_field_overrides_prefilled_headline(self):
        input_field = _FakeLinkedInInput("Stale LinkedIn profile headline")
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        fake_browser_runtime = types.SimpleNamespace(
            human_fill=lambda locator, value: setattr(locator, "_value", value)
        )

        with mock.patch.object(self.mod, "_get_field_label", return_value="Headline"):
            with mock.patch.object(
                self.mod,
                "_answer_for_text_field_details",
                return_value=("Lead PM for AI, ML, and spatial experiences", "generated_application_answer"),
            ):
                with mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}):
                    self.mod._fill_text_field(
                        input_field,
                        modal=object(),
                        payload={},
                        out_dir=Path("/tmp"),
                        filled_steps=filled_steps,
                        unknown_questions=unknown_questions,
                    )

        self.assertEqual(input_field._value, "Lead PM for AI, ML, and spatial experiences")
        self.assertTrue(input_field.cleared)
        self.assertEqual(filled_steps[0]["source"], "generated_application_answer")
        self.assertEqual(unknown_questions, [])

    def test_fill_text_field_overrides_invalid_prose_prefill_during_numeric_retry(self):
        input_field = _FakeLinkedInInput("I have built integrations across enterprise platforms.")
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        fake_browser_runtime = types.SimpleNamespace(
            human_fill=lambda locator, value: setattr(locator, "_value", value)
        )

        with mock.patch.object(self.mod, "_get_field_label", return_value="Integrations"):
            with mock.patch.object(
                self.mod,
                "_answer_for_text_field_details",
                return_value=("5", "generated_application_answer"),
            ):
                with mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}):
                    self.mod._fill_text_field(
                        input_field,
                        modal=object(),
                        payload={},
                        out_dir=Path("/tmp"),
                        filled_steps=filled_steps,
                        unknown_questions=unknown_questions,
                        validation_errors=["Enter a decimal number larger than 0.0"],
                    )

        self.assertEqual(input_field._value, "5")
        self.assertTrue(input_field.cleared)
        self.assertEqual(filled_steps[0]["value"], "5")
        self.assertEqual(unknown_questions, [])

    def test_fill_text_field_clears_conditional_followup_and_skips_unknown(self):
        input_field = _FakeLinkedInInput("Jerrison")
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with (
            mock.patch.object(
                self.mod,
                "_get_field_label",
                return_value=(
                    "If yes, please provide their name(s), their relationship to you and department(s) "
                    "they work in (if known):"
                ),
            ),
            mock.patch.object(self.mod, "_replace_field_value") as replace_value,
            mock.patch.object(self.mod, "_answer_for_text_field_details") as answer_for_text,
        ):
            self.mod._fill_text_field(
                input_field,
                modal=object(),
                payload={},
                out_dir=Path("/tmp"),
                filled_steps=filled_steps,
                unknown_questions=unknown_questions,
            )

        replace_value.assert_called_once_with(input_field, "")
        answer_for_text.assert_not_called()
        self.assertEqual(filled_steps, [])
        self.assertEqual(unknown_questions, [])

    def test_answer_for_select_returns_yes_for_authorization(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Are you legally authorized to work in the United States?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert result == "Yes"

    def test_answer_for_select_returns_no_for_sponsorship(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Will you require sponsorship?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert result == "No"

    def test_answer_for_select_returns_yes_for_hybrid_setting(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Are you comfortable working in a hybrid setting?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert result == "Yes"

    def test_answer_for_select_details_uses_shared_policy_source_for_hybrid_setting(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Are you comfortable working in a hybrid setting?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "shared_positive_fit_policy"

    def test_answer_for_select_returns_yes_for_extensive_experience(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Do you have extensive experience working with Data Science and AI?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert result == "Yes"

    def test_answer_for_select_details_uses_shared_policy_source_for_extensive_experience(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Do you have extensive experience working with Data Science and AI?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "shared_positive_fit_policy"

    def test_answer_for_select_details_uses_shared_policy_source_for_plain_referral_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Were you referred for this position?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "No"
        assert source == "application_profile.md"

    def test_answer_for_select_details_returns_yes_for_boolean_country_work_authorization_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            (
                "Are you legally authorized to work in the country in which this role is located?\n"
                "Are you legally authorized to work in the country in which this role is located?\n \nRequired"
            ),
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "application_profile.md"

    def test_answer_for_select_details_does_not_treat_machine_learning_prompt_as_source_question(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            (
                "Have you worked on a product that incorporated AI, machine learning, or generative AI "
                "capabilities into the core user experience or workflow?"
            ),
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "shared_positive_fit_policy"

    def test_answer_for_select_details_uses_profile_value_for_veteran_binary_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Are you a veteran?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "No"
        assert source == "application_profile.md"

    def test_answer_for_select_details_uses_profile_value_for_hispanic_binary_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Are you Hispanic or Latino?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "application_profile.md"

    def test_answer_for_select_details_uses_profile_value_for_pronoun_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "What is your preferred pronoun?",
            ["He/Him", "She/Her", "They/Them"],
            {},
            Path("/tmp"),
        )
        assert answer == "He/Him"
        assert source == "application_profile.md"

    def test_answer_for_select_details_answers_no_for_interview_accommodation(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Will you require a reasonable accommodation to complete the hiring process which may include technical testing, virtual and in-person style interviews?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "No"
        assert source == "deterministic"

    def test_answer_for_select_details_prefers_candidate_city_for_location_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Which location are you applying for?",
            ["New York, NY", "San Francisco, CA"],
            {"candidate_location": "San Francisco, CA"},
            Path("/tmp"),
        )
        assert answer == "San Francisco, CA"
        assert source == "application_profile.md"

    def test_answer_for_select_details_uses_profile_country_for_phone_country_code_prompt(self):
        mod = load_module("autofill_linkedin_phone_country", "scripts/autofill_linkedin.py")

        with mock.patch("application_submit_common.generate_application_answers", return_value={}):
            answer, source = mod._answer_for_select_details(
                "Phone country code\nPhone country code",
                ["Canada (+1)", "United States (+1)"],
                {},
                Path("/tmp"),
            )

        assert answer == "United States (+1)"
        assert source == "application_profile.md"

    def test_answer_for_select_details_answers_yes_for_salary_requirements_confirmation_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Does the listed salary meet your compensation requirements?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "Yes"
        assert source == "application_profile.md"

    def test_answer_for_select_details_answers_no_for_mixed_sponsorship_visa_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            (
                "Will you now, or at any time in the reasonably foreseeable future, require an employer to "
                "proceed with an immigration case in order to legally employ you? This is sometimes called "
                '"sponsorship" for employment-based immigration status, such as H-1B visa.'
            ),
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "No"
        assert source == "application_profile.md"

    def test_answer_for_select_details_answers_no_for_mixed_work_authorization_or_sponsorship_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Will you now or in the future require any work authorization or sponsorship?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )
        assert answer == "No"
        assert source == "application_profile.md"

    def test_answer_for_select_details_sanitizes_multiline_field_name_for_generated_answers(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        captured = {}

        def fake_generate_application_answers(*, question_specs, **kwargs):
            captured["field_name"] = question_specs[0]["field_name"]
            return {captured["field_name"]: "jerrisonli@gmail.com"}

        with mock.patch("application_submit_common.generate_application_answers", side_effect=fake_generate_application_answers):
            answer, source = mod._answer_for_select_details(
                "Email address\nEmail address",
                ["Select an option", "jerrisonli@gmail.com"],
                {},
                Path("/tmp"),
            )

        assert captured["field_name"] == "email_address_email_address"
        assert answer == "jerrisonli@gmail.com"
        assert source == "generated_application_answer"

    def test_answer_for_select_details_uses_resume_backed_skill_confirmation_for_supported_choice_question(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        label = "Do you have moderate SQL experience?"
        answer, source = mod._answer_for_select_details(
            label,
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )

        assert answer == "Yes"
        assert source == "master_resume.md"

    def test_answer_for_select_details_keeps_citizenship_prompt_on_manual_path(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answer, source = mod._answer_for_select_details(
            "Are you a citizen of the country you'll be employed in?",
            ["Yes", "No"],
            {},
            Path("/tmp"),
        )

        assert answer is None
        assert source == "manual_review_required"

    def test_answer_for_text_field_details_fills_mailing_address_from_location(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        value, source = mod._answer_for_text_field_details(
            "What is your primary mailing address?",
            {},
            Path("/tmp"),
            input_type="text",
        )
        assert value == "San Francisco, CA"
        assert source == "application_profile.md"

    def test_answer_for_text_field_details_coerces_generated_numeric_years(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        label = "How many years of work experience do you have with product management?"
        field_name = mod._linkedin_field_name(label)

        with mock.patch(
            "application_submit_common.generate_application_answers", return_value={field_name: "12+ years"}
        ):
            value, source = mod._answer_for_text_field_details(
                label,
                {},
                Path("/tmp"),
                input_type="number",
            )

        assert value == "12"
        assert source == "generated_application_answer"

    def test_answer_for_text_field_details_rephrases_numeric_retry_prompt_for_short_skill_label(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        prompt_label = "How many years of work experience do you have with Integrations?"
        field_name = mod._linkedin_field_name(prompt_label)

        with mock.patch(
            "application_submit_common.generate_application_answers",
            return_value={field_name: "Approximately 4.5 years"},
        ) as generate_answers:
            value, source = mod._answer_for_text_field_details(
                "Integrations",
                {},
                Path("/tmp"),
                input_type="number",
                force_numeric_experience=True,
            )

        assert value == "4"
        assert source == "generated_application_answer"
        question_specs = generate_answers.call_args.kwargs["question_specs"]
        assert question_specs[0]["label"] == prompt_label

    def test_answer_for_text_field_details_uses_default_skill_years_for_python_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        with mock.patch("application_submit_common.generate_application_answers") as generate_answers:
            value, source = mod._answer_for_text_field_details(
                "How many years of work experience do you have with Python (Programming Language)?",
                {},
                Path("/tmp"),
                input_type="number",
            )

        assert value == "10"
        assert source == "application_profile.md"
        generate_answers.assert_not_called()

    def test_fill_select_field_overrides_wrong_prefilled_location(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class FakeOption:
            def __init__(self, text):
                self._text = text

            def inner_text(self):
                return self._text

        class FakeOptionResults:
            def __init__(self, texts):
                self._texts = texts

            def all(self):
                return [FakeOption(text) for text in self._texts]

        class FakeSelect:
            def __init__(self, texts):
                self._texts = texts
                self.selected_labels = []

            def locator(self, selector):
                assert selector == "option"
                return FakeOptionResults(self._texts)

            def select_option(self, *, label):
                self.selected_labels.append(label)

        filled_steps = []
        unknown_questions = []
        select = FakeSelect(["Select an option", "New York, NY", "San Francisco, CA"])

        with mock.patch.object(mod, "_get_field_label", return_value="Which location are you applying for?"):
            with mock.patch.object(mod, "_selected_non_placeholder_option", return_value="New York, NY"):
                mod._fill_select_field(
                    select,
                    modal=None,
                    payload={"candidate_location": "San Francisco, CA"},
                    out_dir=Path("/tmp"),
                    filled_steps=filled_steps,
                    unknown_questions=unknown_questions,
                )

        assert select.selected_labels == ["San Francisco, CA"]
        assert unknown_questions == []
        assert filled_steps[0]["value"] == "San Francisco, CA"
        assert filled_steps[0]["source"] == "application_profile.md"


class LinkedInBrowserLaunchTests(unittest.TestCase):
    def test_run_easy_apply_wizard_normalizes_linkedin_profile_zoom_before_launch(self):
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = {
                "out_dir": str(out_dir),
                "artifacts": {"page_screenshots_dir": str(out_dir / "pages")},
            }

            class FakeBrowser:
                def __init__(self):
                    self.page = object()
                    self.new_page_calls = []
                    self.closed = False

                def new_page(self, **kwargs):
                    self.new_page_calls.append(kwargs)
                    return self.page

                def close(self):
                    self.closed = True

            fake_browser = FakeBrowser()
            normalized_profiles = []

            def fake_launch(playwright, **kwargs):
                return fake_browser

            def fake_normalize(profile_dir, **kwargs):
                normalized_profiles.append((profile_dir, kwargs))
                return True

            fake_browser_runtime = types.SimpleNamespace(
                normalize_chromium_profile_zoom=fake_normalize,
                launch_chromium_browser=fake_launch,
                submit_slow_mo_ms=lambda headless: 125 if not headless else 0,
                submit_viewport=lambda: {"width": 1360, "height": 900},
            )

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)

            with mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}):
                with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                    with mock.patch.object(mod, "_wizard_flow", return_value=7):
                        result = mod._run_easy_apply_wizard(payload, out_dir, headless=False, submit=False)

            assert result == 7
            assert normalized_profiles == [
                (
                    mod._LINKEDIN_PROFILE_DIR,
                    {
                        "hosts": ("linkedin.com", "www.linkedin.com"),
                        "reset_default_zoom": True,
                    },
                )
            ]
            assert fake_browser.closed is True

    def test_run_easy_apply_wizard_uses_shared_browser_launcher_with_linkedin_profile(self):
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = {
                "out_dir": str(out_dir),
                "artifacts": {"page_screenshots_dir": str(out_dir / "pages")},
            }

            class FakeBrowser:
                def __init__(self):
                    self.page = object()
                    self.new_page_calls = []
                    self.closed = False

                def new_page(self, **kwargs):
                    self.new_page_calls.append(kwargs)
                    return self.page

                def close(self):
                    self.closed = True

            fake_browser = FakeBrowser()
            launch_calls = []

            def fake_launch(playwright, **kwargs):
                launch_calls.append((playwright, kwargs))
                return fake_browser

            fake_browser_runtime = types.SimpleNamespace(
                normalize_chromium_profile_zoom=lambda *_args, **_kwargs: True,
                launch_chromium_browser=fake_launch,
                submit_slow_mo_ms=lambda headless: 125 if not headless else 0,
                submit_viewport=lambda: {"width": 1360, "height": 900},
            )

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)

            with mock.patch.dict(sys.modules, {"browser_runtime": fake_browser_runtime}):
                with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                    with mock.patch.object(mod, "_wizard_flow", return_value=7) as wizard_flow:
                        result = mod._run_easy_apply_wizard(payload, out_dir, headless=False, submit=False)

            assert result == 7
            assert len(launch_calls) == 1
            _, kwargs = launch_calls[0]
            assert kwargs["channel_env_var"] == "JOB_ASSETS_SUBMIT_BROWSER_CHANNEL"
            assert kwargs["executable_env_var"] == "JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE"
            assert kwargs["persistent_profile_dir"] == str(mod._LINKEDIN_PROFILE_DIR)
            assert kwargs["prefer_local_browser"] is True
            assert kwargs["viewport"] == {"width": 1360, "height": 900}
            assert fake_browser.new_page_calls == [
                {"viewport": {"width": 1360, "height": 900}, "device_scale_factor": 2}
            ]
            wizard_flow.assert_called_once_with(
                fake_browser.page,
                payload,
                out_dir,
                False,
                pages_dir=out_dir / "pages",
            )
            assert fake_browser.closed is True


class LinkedInCheckboxTests(unittest.TestCase):
    def test_get_field_label_does_not_use_wrapper_text_for_non_checkbox_fields(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class FakeElement:
            def get_attribute(self, name: str):
                if name == "name":
                    return "field"
                return None

            def locator(self, selector: str):
                assert selector == "xpath=ancestor::label"
                return _FakeLinkedInLocator(count_value=0)

            def evaluate(self, expression: str):
                del expression
                return "Are you authorized to work in the United States?\nYes\nNo"

        label = mod._get_field_label(FakeElement(), object())

        assert label == "field"

    def test_fill_checkbox_checks_terms_and_conditions_gate(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class FakeCheckbox:
            def __init__(self):
                self.checked = False
                self.clicks = 0

            def is_checked(self):
                return self.checked

            def click(self, **kwargs):
                self.checked = True
                self.clicks += 1

        cb = FakeCheckbox()
        filled_steps = []
        with mock.patch.object(mod, "_get_field_label", return_value="I Agree Terms & Conditions"):
            mod._fill_checkbox(cb, object(), {}, filled_steps)

        assert cb.clicks == 1
        assert filled_steps == [
            {
                "field_name": "i_agree_terms_conditions",
                "label": "I Agree Terms & Conditions",
                "kind": "checkbox",
                "checked": True,
                "source": "deterministic",
                "filled": True,
                "required": False,
            }
        ]

    def test_fill_checkbox_checks_future_opportunities_opt_in_when_wrapper_text_only_available(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class FakeCheckbox:
            def __init__(self):
                self.checked = False
                self.clicks = 0

            def is_checked(self):
                return self.checked

            def click(self, **kwargs):
                self.checked = True
                self.clicks += 1

            def get_attribute(self, name: str):
                if name == "type":
                    return "checkbox"
                if name == "name":
                    return "field"
                return None

            def locator(self, selector: str):
                assert selector == "xpath=ancestor::label"
                return _FakeLinkedInLocator(count_value=0)

            def evaluate(self, expression: str):
                del expression
                return "I consent to be contacted about future job opportunities at FieldAI"

        cb = FakeCheckbox()
        filled_steps = []

        mod._fill_checkbox(cb, object(), {}, filled_steps)

        assert cb.clicks == 1
        assert filled_steps == [
            {
                "field_name": "i_consent_to_be_contacted_about_future_job_opportunities_at_fieldai",
                "label": "I consent to be contacted about future job opportunities at FieldAI",
                "kind": "checkbox",
                "checked": True,
                "source": "deterministic",
                "filled": True,
                "required": False,
            }
        ]

    def test_answer_for_select_returns_yes_for_completed_bachelors_prompt(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        with mock.patch("application_submit_common.generate_application_answers", side_effect=AssertionError):
            result = mod._answer_for_select(
                "Have you completed the following level of education: Bachelor's Degree?",
                ["Yes", "No"],
                {},
                Path("/tmp"),
            )

        assert result == "Yes"

    def test_fill_radio_group_records_preselected_checked_option_without_marking_unknown(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class FakeRadioFieldset:
            def __init__(self, legend_text: str, radio_specs: list[tuple[_FakeFieldsetCheckbox, str]]):
                self.legend_locator = _FakeFieldsetLegendLocator(legend_text)
                self.radio_inputs = _FakeFieldsetInputList([radio for radio, _ in radio_specs])
                self.radio_labels = {
                    radio.input_id: _FakeFieldsetLabelLocator(label_text) for radio, label_text in radio_specs
                }

            def locator(self, selector: str):
                if selector == "legend, span.fb-dash-form-element__label":
                    return self.legend_locator
                if selector == 'input[type="radio"]':
                    return self.radio_inputs
                if selector.startswith('label[for="') and selector.endswith('"]'):
                    input_id = selector[len('label[for="') : -2]
                    return self.radio_labels.get(input_id, _FakeFieldsetLabelLocator(""))
                raise AssertionError(f"Unexpected selector: {selector}")

        yes_radio = _FakeFieldsetCheckbox("yes", checked=True)
        no_radio = _FakeFieldsetCheckbox("no", checked=False)
        fieldset = FakeRadioFieldset(
            "Have you completed the following level of education: Bachelor's Degree?",
            [(yes_radio, "Yes"), (no_radio, "No")],
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with mock.patch.object(mod, "_answer_for_select_details", return_value=(None, "unresolved")):
            mod._fill_radio_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert filled_steps == [
            {
                "field_name": "have_you_completed_the_following_level_of_education_bachelor_s_degree",
                "label": "Have you completed the following level of education: Bachelor's Degree?",
                "kind": "radio",
                "value": "Yes",
                "option": "Yes",
                "source": "pre-filled",
                "filled": True,
                "required": True,
            }
        ]
        assert unknown_questions == []
        assert yes_radio.click_calls == 0
        assert no_radio.click_calls == 0

    def test_fill_checkbox_group_selects_answered_option_for_required_yes_no_checkbox_group(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        yes_checkbox = _FakeFieldsetCheckbox("yes")
        no_checkbox = _FakeFieldsetCheckbox("no")
        fieldset = _FakeLinkedInFieldset(
            "Have you worked on a product that incorporated AI into the workflow?",
            [(yes_checkbox, "Yes"), (no_checkbox, "No")],
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with mock.patch.object(mod, "_answer_for_select_details", return_value=("Yes", "shared_positive_fit_policy")):
            mod._fill_checkbox_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert yes_checkbox.click_calls == 1
        assert no_checkbox.click_calls == 0
        assert filled_steps == [
            {
                "field_name": "have_you_worked_on_a_product_that_incorporated_ai_into_the_workflow",
                "label": "Have you worked on a product that incorporated AI into the workflow?",
                "kind": "checkbox_group",
                "value": "Yes",
                "option": "Yes",
                "source": "shared_positive_fit_policy",
                "filled": True,
                "required": True,
            }
        ]
        assert unknown_questions == []

    def test_fill_checkbox_group_uses_checkbox_aria_labels_when_for_labels_are_missing(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        yes_checkbox = _FakeFieldsetCheckbox("yes", aria_label="Yes")
        no_checkbox = _FakeFieldsetCheckbox("no", aria_label="No")
        fieldset = _FakeLinkedInFieldset(
            "Are you authorized to work in the United States?",
            [(yes_checkbox, ""), (no_checkbox, "")],
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with (
            mock.patch.object(mod, "_answer_for_select_details", return_value=("Yes", "application_profile.md")),
            mock.patch.object(mod, "_answer_for_checkbox_group_details", return_value=(None, "manual_review_required")),
        ):
            mod._fill_checkbox_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert yes_checkbox.click_calls == 1
        assert no_checkbox.click_calls == 0
        assert filled_steps == [
            {
                "field_name": "are_you_authorized_to_work_in_the_united_states",
                "label": "Are you authorized to work in the United States?",
                "kind": "checkbox_group",
                "value": "Yes",
                "option": "Yes",
                "source": "application_profile.md",
                "filled": True,
                "required": True,
            }
        ]
        assert unknown_questions == []

    def test_fill_checkbox_group_uses_single_option_text_when_legend_is_missing_for_careers_opt_in(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        consent_checkbox = _FakeFieldsetCheckbox("consent")
        consent_text = "FieldAI has my consent to contact me about future job opportunities."
        fieldset = _FakeLinkedInFieldset("", [(consent_checkbox, consent_text)])
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with mock.patch("application_submit_common.generate_application_answers", side_effect=AssertionError):
            mod._fill_checkbox_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert consent_checkbox.click_calls == 1
        assert filled_steps == [
            {
                "field_name": "fieldai_has_my_consent_to_contact_me_about_future_job_opportunities",
                "label": consent_text,
                "kind": "checkbox_group",
                "value": consent_text,
                "option": consent_text,
                "source": "deterministic",
                "filled": True,
                "required": True,
            }
        ]
        assert unknown_questions == []

    def test_fill_checkbox_group_marks_unknown_when_no_matching_answer_is_available(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        fieldset = _FakeLinkedInFieldset(
            "Which race or ethnicity best describes you?",
            [(_FakeFieldsetCheckbox("decline"), "I don't wish to answer")],
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with mock.patch.object(mod, "_answer_for_checkbox_group_details", return_value=(None, "manual_review_required")):
            mod._fill_checkbox_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert filled_steps == []
        assert unknown_questions == [
            {
                "field_name": "which_race_or_ethnicity_best_describes_you",
                "label": "Which race or ethnicity best describes you?",
                "kind": "checkbox_group",
            }
        ]

    def test_fill_checkbox_group_supports_multi_select_majors_and_clears_stale_other(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        business_checkbox = _FakeFieldsetCheckbox("business")
        cs_checkbox = _FakeFieldsetCheckbox("cs")
        other_checkbox = _FakeFieldsetCheckbox("other", checked=True)
        fieldset = _FakeLinkedInFieldset(
            "What is your major?",
            [
                (business_checkbox, "Business Administration"),
                (cs_checkbox, "Computer Science/Software Engineering"),
                (other_checkbox, "Other"),
            ],
        )
        filled_steps: list[dict] = []
        unknown_questions: list[dict] = []

        with mock.patch.object(
            mod,
            "_answer_for_checkbox_group_details",
            return_value=(["Business Administration", "Computer Science/Software Engineering"], "application_profile.md"),
        ):
            mod._fill_checkbox_group(fieldset, {}, Path("/tmp"), filled_steps, unknown_questions)

        assert business_checkbox.checked is True
        assert cs_checkbox.checked is True
        assert other_checkbox.checked is False
        assert [step["value"] for step in filled_steps] == [
            "Business Administration",
            "Computer Science/Software Engineering",
        ]
        assert all(step["source"] == "application_profile.md" for step in filled_steps)
        assert unknown_questions == []

    def test_answer_for_checkbox_group_details_uses_profile_value_for_highest_education_level(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        answers, source = mod._answer_for_checkbox_group_details(
            (
                "What is your highest level of completed education\n"
                "What is your highest level of completed education\n \nRequired"
            ),
            ["High school diploma", "GED", "Bachelors Degree", "Masters Degree", "PHD"],
            {},
            Path("/tmp"),
        )

        assert answers == ["Masters Degree"]
        assert source == "application_profile.md"

    def test_answer_for_checkbox_group_details_returns_linkedin_for_source_question(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        with mock.patch("application_submit_common.generate_application_answers", return_value={}):
            answers, source = mod._answer_for_checkbox_group_details(
                "How did you learn about this opportunity?",
                ["Field AI Website", "Employee Referral", "LinkedIn", "Indeed"],
                {},
                Path("/tmp"),
            )

        assert answers == ["LinkedIn"]
        assert source == "hardcoded"


class LinkedInNotEasyApplyResultTests(unittest.TestCase):
    def test_write_job_closed_result(self):
        import json
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = {"job_url": "https://www.linkedin.com/jobs/view/789/"}
            screenshot_path = out_dir / "submit" / "linkedin_job_closed.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_text("png", encoding="utf-8")

            mod._write_job_closed_result(
                out_dir,
                payload,
                screenshot_path=screenshot_path,
                reason="This job is no longer accepting applications.",
            )

            result = json.loads((out_dir / "submit" / "application_submission_result.json").read_text())
            assert result["status"] == "job_closed"
            assert result["failure_type"] == "job_closed"
            assert result["artifacts"]["page_screenshot"] == str(screenshot_path)

    def test_linkedin_job_closed_reason_detects_no_longer_accepting_applications(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

        class _ClosedJobPage:
            def inner_text(self, _selector):
                return "This job is no longer accepting applications on LinkedIn."

        assert mod._linkedin_job_closed_reason(_ClosedJobPage()) == "no longer accepting applications"

    def test_write_not_easy_apply_result_external(self):
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = {"job_url": "https://www.linkedin.com/jobs/view/123/"}
            screenshot_path = out_dir / "submit" / "linkedin_external_apply_page.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_text("png", encoding="utf-8")
            mod._write_not_easy_apply_result(
                out_dir,
                payload,
                reason="external_apply",
                screenshot_path=screenshot_path,
            )
            result_path = out_dir / "submit" / "application_submission_result.json"
            assert result_path.exists()
            import json

            result = json.loads(result_path.read_text())
            assert result["status"] == "not_easy_apply"
            assert result["reason"] == "external_apply"
            assert result["failure_type"] == "external_apply"
            assert (
                result["message"]
                == "LinkedIn job no longer exposes Easy Apply; an external Apply flow is shown instead."
            )
            assert result["job_url"] == "https://www.linkedin.com/jobs/view/123/"
            assert result["artifacts"]["page_screenshot"] == str(screenshot_path)

    def test_write_not_easy_apply_result_no_button(self):
        import tempfile

        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = {"job_url": "https://www.linkedin.com/jobs/view/456/"}
            screenshot_path = out_dir / "submit" / "linkedin_no_apply_debug.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_text("png", encoding="utf-8")
            mod._write_not_easy_apply_result(
                out_dir,
                payload,
                reason="no_apply_button",
                screenshot_path=screenshot_path,
            )
            result_path = out_dir / "submit" / "application_submission_result.json"
            import json

            result = json.loads(result_path.read_text())
            assert result["status"] == "not_easy_apply"
            assert result["reason"] == "no_apply_button"
            assert result["failure_type"] == "no_apply_button"
            assert (
                result["message"] == "LinkedIn job does not currently expose an Easy Apply or external Apply control."
            )
            assert result["artifacts"]["page_screenshot"] == str(screenshot_path)


class LinkedInScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def test_capture_linkedin_surface_screenshot_prefers_visible_modal(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=True)
        main = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False),
            selector_locators={"main.scaffold-layout__main": main},
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "modal.png"
            self.mod._capture_linkedin_surface_screenshot(page, output_path, prefer_modal=True)

        assert modal.screenshot_calls == [{"path": str(output_path), "type": "png"}]
        assert main.screenshot_calls == []
        assert page.screenshot_calls == []

    def test_capture_linkedin_surface_screenshot_builds_composite_modal_capture(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=True)
        header = _FakeLinkedInLocator(visible=True)
        content = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False),
            selector_locators={
                "div.artdeco-modal__header": header,
                "div.jobs-easy-apply-modal__content": content,
                "div.artdeco-modal__content.jobs-easy-apply-modal__content": content,
                "div.artdeco-modal__content": content,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "modal.png"
            stitched_calls = []
            concatenated = []
            with (
                mock.patch.object(
                    self.mod,
                    "capture_scrollable_locator_screenshot",
                    side_effect=lambda page_arg, locator_arg, path_arg: stitched_calls.append(
                        (page_arg, locator_arg, Path(path_arg).name)
                    ),
                ),
                mock.patch.object(
                    self.mod,
                    "concatenate_images_vertically",
                    side_effect=lambda image_paths, output_arg: concatenated.append(
                        ([Path(path).name for path in image_paths], Path(output_arg).name)
                    ),
                ),
            ):
                self.mod._capture_linkedin_surface_screenshot(page, output_path, prefer_modal=True)

        assert len(header.screenshot_calls) == 1
        assert len(stitched_calls) == 1
        assert stitched_calls[0][0] is page
        assert stitched_calls[0][2] == "modal__content.png"
        assert len(concatenated) == 1
        assert len(concatenated[0][0]) == 2
        assert concatenated[0][1] == "modal.png"

    def test_capture_linkedin_surface_screenshot_prefers_structural_surface_over_top_card_only(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=False)
        top_card = _FakeLinkedInLocator(visible=True)
        main = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False),
            selector_locators={
                "div.jobs-unified-top-card": top_card,
                "main.scaffold-layout__main": main,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "main.png"
            with mock.patch.object(self.mod, "capture_full_page") as capture_full_page:
                self.mod._capture_linkedin_surface_screenshot(page, output_path)

        capture_full_page.assert_called_once()
        assert top_card.screenshot_calls == []
        assert page.screenshot_calls == []

    def test_capture_linkedin_surface_screenshot_uses_full_page_capture_for_structural_surface(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=False, count_value=0)
        main = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False, count_value=0),
            selector_locators={"main.scaffold-layout__main": main},
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "main-clip.png"
            with mock.patch.object(self.mod, "capture_full_page") as capture_full_page:
                self.mod._capture_linkedin_surface_screenshot(page, output_path)

        capture_full_page.assert_called_once()
        assert main.screenshot_calls == []
        assert page.screenshot_calls == []

    def test_capture_linkedin_surface_screenshot_falls_back_to_top_card_when_structural_capture_errors(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=False, count_value=0)
        top_card = _FakeLinkedInLocator(visible=True)
        main = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False, count_value=0),
            selector_locators={
                "div.jobs-unified-top-card": top_card,
                "main.scaffold-layout__main": main,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "main-fallback.png"
            with mock.patch.object(self.mod, "capture_full_page", side_effect=RuntimeError("capture failed")):
                self.mod._capture_linkedin_surface_screenshot(page, output_path)

        assert top_card.screenshot_calls == [{"path": str(output_path), "type": "png"}]
        assert page.screenshot_calls == []

    def test_capture_linkedin_surface_screenshot_falls_back_to_page_viewport_when_structural_surface_has_no_box(self):
        import tempfile

        modal = _FakeLinkedInLocator(visible=False, count_value=0)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False),
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "page.png"
            self.mod._capture_linkedin_surface_screenshot(page, output_path, prefer_modal=True)

        assert page.screenshot_calls == [{"path": str(output_path), "full_page": False}]


class LinkedInResumeStateTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        self.payload = {
            "resume_path": "/tmp/Jerrison Li Resume - Asurion.pdf",
            "company": "Asurion",
        }

    def test_resume_text_matches_expected_company_named_resume(self):
        assert self.mod._resume_text_matches_expected(
            "Deselect resume Jerrison Li Resume - Asurion.pdf",
            self.payload,
        )

    def test_classify_linkedin_resume_markers_detects_visible_upload_path_and_wrong_selected_resume(self):
        markers = {
            "modalText": "Resume Upload resume Show more resumes",
            "buttons": [
                "Deselect resume Jerrison Li Resume - Linkedin.pdf",
                "Select resume Jerrison Li Resume.pdf",
                "Upload resume",
            ],
            "checked": ["Deselect resume Jerrison Li Resume - Linkedin.pdf"],
            "fileInputs": [],
        }

        state = self.mod._classify_linkedin_resume_markers(markers, self.payload)

        assert state["resume_step_visible"] is True
        assert state["visible_upload_path"] is True
        assert state["selected_expected"] is False

    def test_classify_linkedin_resume_markers_detects_expected_selected_resume(self):
        markers = {
            "modalText": "Resume Upload resume",
            "buttons": [
                "Deselect resume Jerrison Li Resume - Asurion.pdf",
                "Upload resume",
            ],
            "checked": ["Deselect resume Jerrison Li Resume - Asurion.pdf"],
            "fileInputs": [],
        }

        state = self.mod._classify_linkedin_resume_markers(markers, self.payload)

        assert state["resume_step_visible"] is True
        assert state["visible_upload_path"] is True
        assert state["selected_expected"] is True

    def test_resume_outcomes_report_hidden_control_review_case(self):
        outcomes = self.mod._resume_outcomes(
            {
                "status": "",
                "observed_selection_labels": [],
                "visible_upload_path_seen": False,
            },
            self.payload,
        )

        assert outcomes[0]["status"] == "review_without_visible_resume_controls"
        assert outcomes[0]["expected_file"] == "Jerrison Li Resume - Asurion.pdf"

    def test_resume_outcomes_report_visible_upload_failure(self):
        outcomes = self.mod._resume_outcomes(
            {
                "status": "upload_verification_failed",
                "message": "expected resume was never selected",
                "observed_selection_labels": ["Deselect resume Jerrison Li Resume - Linkedin.pdf"],
                "visible_upload_path_seen": True,
            },
            self.payload,
        )

        assert outcomes[0]["status"] == "upload_verification_failed"
        assert outcomes[0]["observed_selection_labels"] == ["Deselect resume Jerrison Li Resume - Linkedin.pdf"]


class LinkedInFailureArtifactTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def _payload(self, out_dir: Path) -> dict:
        submit_dir = out_dir / "submit"
        pages_dir = submit_dir / "linkedin_autofill_pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        return {
            "job_url": "https://www.linkedin.com/jobs/view/1234567890/",
            "company": "Acme",
            "job_title": "Principal Product Manager",
            "artifacts": {
                "report_markdown": str(submit_dir / "linkedin_autofill_report.md"),
                "report_json": str(submit_dir / "linkedin_autofill_report.json"),
                "pre_submit_screenshot": str(submit_dir / "linkedin_autofill_pre_submit.png"),
                "post_submit_screenshot": str(submit_dir / "linkedin_autofill_post_submit.png"),
                "page_screenshots_dir": str(pages_dir),
                "unknown_questions_json": str(submit_dir / "linkedin_unknown_questions.json"),
                "submit_debug_html": str(submit_dir / "linkedin_submit_debug.html"),
                "submit_debug_screenshot": str(submit_dir / "linkedin_submit_debug.png"),
                "payload_json": str(submit_dir / "linkedin_autofill_payload.json"),
            },
        }

    def test_clear_current_attempt_linkedin_artifacts_removes_stale_review_outputs_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = self._payload(out_dir)
            artifacts = payload["artifacts"]

            for key in (
                "report_markdown",
                "report_json",
                "pre_submit_screenshot",
                "post_submit_screenshot",
                "submit_debug_html",
                "submit_debug_screenshot",
            ):
                path = Path(artifacts[key])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("stale", encoding="utf-8")

            payload_json = Path(artifacts["payload_json"])
            payload_json.write_text("{}", encoding="utf-8")

            unknown_questions = Path(artifacts["unknown_questions_json"])
            unknown_questions.write_text("[]", encoding="utf-8")

            result_path = out_dir / "submit" / "application_submission_result.json"
            result_path.write_text("{}", encoding="utf-8")

            page_shot = Path(artifacts["page_screenshots_dir"]) / "page_01.png"
            page_shot.write_text("png", encoding="utf-8")

            self.mod._clear_current_attempt_linkedin_artifacts(payload)

            assert not Path(artifacts["report_markdown"]).exists()
            assert not Path(artifacts["report_json"]).exists()
            assert not Path(artifacts["pre_submit_screenshot"]).exists()
            assert not Path(artifacts["post_submit_screenshot"]).exists()
            assert not Path(artifacts["submit_debug_html"]).exists()
            assert not Path(artifacts["submit_debug_screenshot"]).exists()
            assert not result_path.exists()
            assert not page_shot.exists()

            assert payload_json.exists()
            assert unknown_questions.exists()

    def test_write_failed_result_persists_classified_linkedin_failure_with_artifacts(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = self._payload(out_dir)
            debug_screenshot = Path(payload["artifacts"]["submit_debug_screenshot"])
            debug_screenshot.parent.mkdir(parents=True, exist_ok=True)
            debug_screenshot.write_text("debug", encoding="utf-8")

            step_screenshot = Path(payload["artifacts"]["page_screenshots_dir"]) / "debug_step_02.png"
            step_screenshot.write_text("step", encoding="utf-8")

            self.mod._write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_modal_missing",
                message="LinkedIn Easy Apply modal not visible at step 2.",
                retry_class="targeted_retry",
                step_num=2,
                step_screenshot=step_screenshot,
            )

            result_path = out_dir / "submit" / "application_submission_result.json"
            assert result_path.exists()

            result = json.loads(result_path.read_text(encoding="utf-8"))
            assert result["status"] == "failed"
            assert result["board"] == "linkedin"
            assert result["failure_type"] == "linkedin_modal_missing"
            assert result["retry_class"] == "targeted_retry"
            assert result["step_num"] == 2
            assert result["job_url"] == payload["job_url"]
            assert result["company"] == "Acme"
            assert result["job_title"] == "Principal Product Manager"
            assert result["artifacts"]["submit_debug_screenshot"] == str(debug_screenshot)
            assert result["artifacts"]["step_screenshot"] == str(step_screenshot)
            assert result["updated_at_utc"].endswith("+00:00")


class LinkedInModalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def test_easy_apply_button_prefers_primary_job_detail_apply_link_over_sidebar_matches(self):
        modal = _FakeLinkedInLocator(visible=False)
        sidebar_easy_apply = _FakeLinkedInLocator(visible=True)
        real_easy_apply = _FakeLinkedInLocator(visible=True)
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=sidebar_easy_apply,
            selector_locators={
                'button.jobs-apply-button:has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Easy Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply to this job"]': real_easy_apply,
            },
        )

        selected = self.mod._easy_apply_button(page)

        assert selected is real_easy_apply

    def test_easy_apply_button_ignores_recommended_job_cards_when_generic_match_text_is_not_exact(self):
        modal = _FakeLinkedInLocator(visible=False)
        recommended_easy_apply_cards = _FakeLinkedInLocator(
            items=[
                _FakeLinkedInLocator(
                    visible=True,
                    inner_text_value=(
                        "Senior Product Manager, Growth & Consumer App Snaplii "
                        "San Francisco Bay Area (Remote) 1 week ago Easy Apply"
                    ),
                ),
                _FakeLinkedInLocator(
                    visible=True,
                    inner_text_value=(
                        "Founding Product Manager Stealth Startup "
                        "San Francisco Bay Area (On-site) 2 days ago Easy Apply"
                    ),
                ),
            ]
        )
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False, count_value=0),
            selector_locators={
                'button.jobs-apply-button:has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Easy Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Easy Apply"]': _FakeLinkedInLocator(visible=False, count_value=0),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button:has-text("Easy Apply")': _FakeLinkedInLocator(visible=False, count_value=0),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")': recommended_easy_apply_cards,
            },
        )

        selected = self.mod._easy_apply_button(page)

        assert selected.count() == 0

    def test_easy_apply_button_accepts_generic_exact_text_fallback_when_primary_button_has_no_strict_marker(self):
        modal = _FakeLinkedInLocator(visible=False)
        exact_text_easy_apply = _FakeLinkedInLocator(visible=True, inner_text_value="Easy Apply")
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False, count_value=0),
            selector_locators={
                'button.jobs-apply-button:has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Easy Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Easy Apply"]': _FakeLinkedInLocator(visible=False, count_value=0),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button:has-text("Easy Apply")': _FakeLinkedInLocator(visible=False, count_value=0),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")': _FakeLinkedInLocator(
                    items=[exact_text_easy_apply]
                ),
            },
        )

        selected = self.mod._easy_apply_button(page)

        assert selected is exact_text_easy_apply

    def test_external_apply_button_ignores_recommended_job_cards_when_primary_button_is_apply(self):
        modal = _FakeLinkedInLocator(visible=False)
        primary_apply = _FakeLinkedInLocator(visible=True, inner_text_value="Apply", aria_label="Apply to this job")
        recommended_easy_apply_cards = _FakeLinkedInLocator(
            items=[
                _FakeLinkedInLocator(
                    visible=True,
                    inner_text_value=(
                        "Senior Product Manager, Growth & Consumer App Snaplii "
                        "San Francisco Bay Area (Remote) 1 week ago Easy Apply"
                    ),
                ),
            ]
        )
        page = _FakeLinkedInPage(
            modal=modal,
            easy_apply_button=_FakeLinkedInLocator(visible=False, count_value=0),
            external_apply_button=primary_apply,
            selector_locators={
                'button.jobs-apply-button:has-text("Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Apply")': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'button[aria-label*="Apply to this job"]': _FakeLinkedInLocator(
                    visible=False,
                    count_value=0,
                ),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Apply to this job"]': primary_apply,
                'button[aria-label*="Apply"]': _FakeLinkedInLocator(visible=False, count_value=0),
                'a[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Apply")': recommended_easy_apply_cards,
            },
        )

        selected = self.mod._external_apply_button(page)

        assert selected is primary_apply

    def test_attempt_reopen_easy_apply_modal_clicks_easy_apply_again_when_modal_is_missing(self):
        modal = _FakeLinkedInLocator(visible=False)
        easy_apply = _FakeLinkedInLocator(visible=True, on_click=lambda: setattr(modal, "visible", True))
        page = _FakeLinkedInPage(modal=modal, easy_apply_button=easy_apply)

        with mock.patch.object(self.mod, "_dismiss_discard_dialog", return_value=False):
            reopened = self.mod._attempt_reopen_easy_apply_modal(page, step_num=4)

        assert reopened is True
        assert easy_apply.click_count == 1
        assert easy_apply.scroll_count == 1
        assert page.wait_calls == [1500]

    def test_attempt_reopen_easy_apply_modal_returns_false_when_easy_apply_button_is_not_visible(self):
        modal = _FakeLinkedInLocator(visible=False)
        easy_apply = _FakeLinkedInLocator(visible=False)
        page = _FakeLinkedInPage(modal=modal, easy_apply_button=easy_apply)

        with mock.patch.object(self.mod, "_dismiss_discard_dialog", return_value=False):
            reopened = self.mod._attempt_reopen_easy_apply_modal(page, step_num=1)

        assert reopened is False
        assert easy_apply.click_count == 0
