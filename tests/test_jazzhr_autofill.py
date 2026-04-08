import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_jazzhr_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_for_url_detects_jazzhr_applytojob_host():
    url = "https://jobs.applytojob.com/apply/12345/senior-product-manager"
    assert looks_like_jazzhr_url(url)
    assert _board_for_url(url) == "jazzhr"


def test_build_payload_marks_board_jazzhr(tmp_path):
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")
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
                "jd_source": "https://jobs.applytojob.com/apply/12345/senior-product-manager",
                "company": "bitpay",
                "company_proper": "BitPay",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Candidate",
                last_name="Name",
                email="candidate@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "jazzhr"


def test_classify_submit_state_detects_applytojob_confirmation():
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")

    state = autofill._classify_submit_state(
        {
            "page_text": "Application Submitted",
            "url": "https://bitpay.applytojob.com/apply/jobs/details/123",
            "errors": [],
            "invalid_fields": [],
            "recaptcha_visible": False,
            "recaptcha_challenge_active": False,
        }
    )
    assert state["status"] == "confirmed"


def test_click_apply_if_needed_clicks_apply_now_when_only_hidden_form_shell_exists():
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")

    class _Locator:
        def __init__(self, count: int, visible: bool = True):
            self._count = count
            self._visible = visible
            self.click_calls = 0
            self.scroll_calls = 0
            self.first = self

        def count(self):
            return self._count

        def is_visible(self):
            return self._visible

        def scroll_into_view_if_needed(self):
            self.scroll_calls += 1

        def click(self):
            self.click_calls += 1

    apply_link = _Locator(1, visible=True)

    class _Page:
        def __init__(self):
            self.waits = []

        def locator(self, selector):
            assert selector == autofill._VISIBLE_FORM_SELECTOR
            return _Locator(0, visible=False)

        def get_by_role(self, role, name=None):
            assert name is not None
            if role == "link" and hasattr(name, "search") and name.search("APPLY NOW"):
                return apply_link
            return _Locator(0, visible=False)

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = _Page()

    autofill._click_apply_if_needed(page)

    assert apply_link.scroll_calls == 1
    assert apply_link.click_calls == 1
    assert page.waits == [2000]


def test_click_apply_if_needed_skips_when_visible_form_controls_already_exist():
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")

    class _Locator:
        def __init__(self, count: int, visible: bool = True):
            self._count = count
            self._visible = visible
            self.first = self

        def count(self):
            return self._count

        def is_visible(self):
            return self._visible

    class _Page:
        def __init__(self):
            self.role_queries = []

        def locator(self, selector):
            assert selector == autofill._VISIBLE_FORM_SELECTOR
            return _Locator(1, visible=True)

        def get_by_role(self, role, name=None):
            self.role_queries.append((role, getattr(name, "pattern", name)))
            raise AssertionError("apply CTA should not be queried once visible JazzHR fields exist")

    page = _Page()

    autofill._click_apply_if_needed(page)

    assert page.role_queries == []


def test_sync_live_fields_updates_standard_steps_and_records_required_unknown_questions(tmp_path):
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")

    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    payload_path = submit_dir / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "steps": [
                    {"field_name": "first_name", "label": "First name", "kind": "text", "required": True, "value": "Candidate"},
                    {"field_name": "work_authorization", "label": "Work authorization", "kind": "select", "required": False, "value": "Yes"},
                ],
                "unknown_questions": [],
            }
        ),
        encoding="utf-8",
    )

    class _Page:
        def evaluate(self, _script):
            return [
                {
                    "label": "First name*",
                    "kind": "text",
                    "required": True,
                    "name": "resumator-firstname-value",
                    "id": "resumator-firstname-value",
                    "options": [],
                },
                {
                    "label": "Are you legally authorized to work in the United States?*",
                    "kind": "select",
                    "required": True,
                    "name": "resumator-questionnaire[1]",
                    "id": "resumator-questionnaire-q1",
                    "options": ["-- No answer --", "Yes", "No"],
                },
                {
                    "label": "Do you have 5+ years of product management experience specifically in payments?*",
                    "kind": "select",
                    "required": True,
                    "name": "resumator-questionnaire[2]",
                    "id": "resumator-questionnaire-q2",
                    "options": ["-- No answer --", "Yes", "No"],
                },
            ]

    with (
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Candidate",
                last_name="Name",
                email="candidate@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
                verification_code_email=None,
            ),
        ),
        mock.patch.object(
            autofill,
            "_infer_deterministic",
            side_effect=lambda label, options: "Yes" if "legally authorized" in label else None,
        ),
    ):
        autofill._sync_live_fields(_Page(), payload_path)

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    first_name_step = next(step for step in payload["steps"] if step["value"] == "Candidate")
    assert first_name_step["field_name"] == "resumator-firstname-value"
    work_auth_step = next(step for step in payload["steps"] if step["field_name"] == "resumator-questionnaire[1]")
    assert work_auth_step["value"] == "Yes"
    assert payload["unknown_questions"] == [
        {
            "field_name": "resumator-questionnaire[2]",
            "label": "Do you have 5+ years of product management experience specifically in payments?*",
            "kind": "select",
            "required": True,
            "status": "planned",
            "source": "live_application_form",
            "reason": "The live JazzHR form contains a required question without a deterministic repo-backed answer in the current payload.",
            "note": "Discovered from the visible JazzHR application form during draft rerun.",
        }
    ]
    unknown_path = submit_dir / autofill._BOARD_CONSTANTS["unknown_questions_json"]
    assert unknown_path.exists()


def test_run_browser_registers_form_ready_fn():
    autofill = load_module("autofill_jazzhr", "scripts/autofill_jazzhr.py")

    with mock.patch("autofill_pipeline.run_browser_pipeline", return_value=0) as run_browser_pipeline:
        rc = autofill._run_browser(Path("/tmp/payload.json"), headless=True, submit=False)

    assert rc == 0
    kwargs = run_browser_pipeline.call_args.kwargs
    assert callable(kwargs["form_ready_fn"])
    assert callable(kwargs["post_navigate_hook"])
