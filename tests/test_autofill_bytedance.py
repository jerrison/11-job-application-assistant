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


def test_bytedance_application_url_from_detail():
    autofill = load_module("autofill_bytedance", "scripts/autofill_bytedance.py")

    assert (
        autofill._bytedance_application_url(
            "https://jobs.bytedance.com/en/position/7613140316427045125/detail"
            "?utm_source=trueup.io&utm_medium=website&ref=trueup"
        )
        == "https://jobs.bytedance.com/en/resume/7613140316427045125/apply"
    )


def test_detect_bytedance_auth_result_for_sign_in_gate():
    autofill = load_module("autofill_bytedance", "scripts/autofill_bytedance.py")

    class _Body:
        def inner_text(self, **kwargs):
            del kwargs
            return """
            Sign in
            Sign in with Email
            Sign in with Mobile
            Forgot password?
            """

    class _Page:
        url = "https://jobs.bytedance.com/en/login?redirect_path=%2Fresume%2F7613140316427045125%2Fapply"

        def locator(self, selector: str):
            assert selector == "body"
            return _Body()

    result = autofill._detect_bytedance_auth_result(
        _Page(),
        {
            "job_url": "https://jobs.bytedance.com/en/resume/7613140316427045125/apply",
            "company": "ByteDance",
            "job_title": "Senior Product Manager (Multiple Positions)",
        },
    )

    assert result is not None
    assert result["status"] == "skipped_auth"
    assert result["failure_type"] == "auth_guarded"
    assert result["auth_state"] == "sign_in_gate"
    assert result["auth_scope"] == "bytedance:jobs.bytedance.com"


def test_detect_bytedance_live_surface_result_for_rendered_application_shell():
    autofill = load_module("autofill_bytedance", "scripts/autofill_bytedance.py")

    class _Body:
        def inner_text(self, **kwargs):
            del kwargs
            return """
            Join ByteDance
            Resume
            Basic Information
            Attachment
            Email
            Mobile
            """

    class _Page:
        url = "https://jobs.bytedance.com/en/resume/7613140316427045125/apply"

        def locator(self, selector: str):
            assert selector == "body"
            return _Body()

    result = autofill._detect_bytedance_live_surface_result(
        _Page(),
        {
            "job_url": "https://jobs.bytedance.com/en/resume/7613140316427045125/apply",
            "company": "ByteDance",
            "job_title": "Senior Product Manager (Multiple Positions)",
        },
    )

    assert result is not None
    assert result["status"] == "unknown"
    assert result["failure_type"] == "unsupported"


def test_run_browser_registers_post_navigate_hook():
    autofill = load_module("autofill_bytedance", "scripts/autofill_bytedance.py")

    with mock.patch("autofill_pipeline.run_browser_pipeline", return_value=0) as run_browser_pipeline:
        rc = autofill._run_browser(Path("/tmp/payload.json"), headless=True, submit=False)

    assert rc == 0
    kwargs = run_browser_pipeline.call_args.kwargs
    assert callable(kwargs["post_navigate_hook"])
