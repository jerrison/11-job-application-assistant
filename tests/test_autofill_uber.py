import importlib.util
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


def test_detect_uber_auth_result_for_account_gate():
    autofill = load_module("autofill_uber", "scripts/autofill_uber.py")

    class _Body:
        def inner_text(self, **kwargs):
            return """
            Uber Careers account
            Create account
            Sign in
            Continue with Google
            """

    class _Page:
        url = "https://www.uber.com/careers/apply/form/123456"

        def locator(self, selector: str):
            assert selector == "body"
            return _Body()

    result = autofill._detect_uber_auth_result(
        _Page(),
        {
            "job_url": "https://www.uber.com/careers/apply/form/123456",
            "company": "Uber",
            "job_title": "Sr Product Manager, Trip Safety",
        },
    )

    assert result is not None
    assert result["status"] == "skipped_auth"
    assert result["failure_type"] == "auth_guarded"
    assert result["auth_state"] == "account_gate"


def test_run_browser_registers_post_navigate_hook():
    autofill = load_module("autofill_uber", "scripts/autofill_uber.py")

    with mock.patch("autofill_pipeline.run_browser_pipeline", return_value=0) as run_browser_pipeline:
        rc = autofill._run_browser(Path("/tmp/payload.json"), headless=True, submit=False)

    assert rc == 0
    kwargs = run_browser_pipeline.call_args.kwargs
    assert callable(kwargs["post_navigate_hook"])
