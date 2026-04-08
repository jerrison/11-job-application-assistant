#!/usr/bin/env python3
"""Dispatch browser-based application submission by job-board host."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from answer_state_sync import sync_current_attempt_answer_states_from_proof  # noqa: E402
from application_submit_common import (  # noqa: E402
    PENDING_USER_INPUT_JSON,
    find_output_dir,
    load_meta,
    load_pending_user_input_for_submit_attempt,
    preferred_meta_job_url,
)
from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE  # noqa: E402
from entrypoint_guard import abort_if_recursive_entrypoints_forbidden  # noqa: E402
from job_board_urls import (  # noqa: E402
    html_looks_like_recruitee,
    looks_like_ashby_wrapper_url,
    looks_like_avature_url,
    looks_like_breezy_url,
    looks_like_bytedance_url,
    looks_like_dover_url,
    looks_like_greenhouse_url,
    looks_like_icims_url,
    looks_like_jazzhr_url,
    looks_like_jobvite_url,
    looks_like_oracle_hcm_url,
    looks_like_paycor_url,
    looks_like_phenom_url,
    looks_like_recruitee_url,
    looks_like_recruitee_wrapper_url,
    looks_like_successfactors_url,
)
from llm_provider import VALID_PROVIDERS  # noqa: E402
from output_layout import (  # noqa: E402
    ACTIVE_SUBMIT_DIR_ENV,
    active_submit_dir_name,
    ensure_reapply_submit_dir,
    migrate_role_output_layout,
    preferred_submit_dir_name_for_post_submit,
    role_submit_path,
    submit_dirs_by_mtime,
)
from project_env import load_project_env  # noqa: E402
from runtime_entrypoints import python_script_command  # noqa: E402
from runtime_policy import ensure_action_allowed  # noqa: E402
from runtime_trace import configure_runtime_trace, emit_trace  # noqa: E402
from worker_subprocess import run_worker_subprocess  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent
GEM_HOST_PATTERNS = ("jobs.gem.com",)
LEVER_HOST_PATTERNS = ("lever.co",)
WORKDAY_HOST_PATTERNS = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
)
ASHBY_HOST_PATTERNS = ("ashbyhq.com",)
DOVER_HOST_PATTERNS = ("app.dover.com",)
ICIMS_HOST_PATTERNS = ("icims.com",)
AVATURE_HOST_PATTERNS = ("avature.net",)
ORACLE_HCM_HOST_PATTERNS = ("oraclecloud.com",)
EIGHTFOLD_HOST_PATTERNS = ("eightfold.ai",)
SMARTRECRUITERS_HOST_PATTERNS = ("smartrecruiters.com",)
WORKABLE_HOST_PATTERNS = ("workable.com",)
COMEET_HOST_PATTERNS = ("comeet.com", "comeet.co")
BAMBOOHR_HOST_PATTERNS = ("bamboohr.com",)
RIPPLING_HOST_PATTERNS = ("ats.rippling.com",)
UBER_HOST_PATTERNS = ("uber.com",)
MOTIONRECRUITMENT_HOST_PATTERNS = ("motionrecruitment.com",)
REDUCTO_HOST_PATTERNS = ("reducto.ai",)
BYTEDANCE_HOST_PATTERNS = ("jobs.bytedance.com", "joinbytedance.com")
LINKEDIN_HOST_PATTERNS = ("linkedin.com",)
SUBMISSION_RESULT_JSON = "application_submission_result.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
DEFAULT_PENDING_EMAIL_RECHECK_SECONDS = 300


load_project_env()
abort_if_recursive_entrypoints_forbidden("scripts/submit_application.py")


def _clear_stale_current_attempt_terminal_artifacts(out_dir: Path, submit_dirname: str) -> None:
    submit_dir = out_dir / submit_dirname
    for stale_name in (
        SUBMISSION_RESULT_JSON,
        PENDING_USER_INPUT_JSON,
        "awaiting_captcha.json",
    ):
        try:
            (submit_dir / stale_name).unlink(missing_ok=True)
        except OSError:
            continue


def _sync_current_attempt_answer_states(out_dir: Path, submit_dirname: str) -> None:
    sync_current_attempt_answer_states_from_proof(
        out_dir,
        submit_dirname,
        allow_pending_override=True,
    )


def _refresh_draft_review_artifacts(out_dir: Path, submit_dirname: str, meta: dict) -> None:
    submit_dir = out_dir / submit_dirname
    if not submit_dir.is_dir():
        return
    try:
        from draft_manager import generate_draft_summary

        generate_draft_summary(out_dir, submit_dir, meta)
    except Exception as exc:
        print(
            f"WARNING: Failed to refresh draft review artifacts for {_display_path(out_dir)}: {exc}",
            file=sys.stderr,
        )


def _finalize_non_submit_attempt(out_dir: Path, submit_dirname: str, meta: dict) -> None:
    _sync_current_attempt_answer_states(out_dir, submit_dirname)
    _refresh_draft_review_artifacts(out_dir, submit_dirname, meta)


def _html_looks_like_eightfold(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "window._ef_product",
        "pcsx-data",
        "eightfold-font-base.css",
        "static.vscdn.net/fonts/css/eightfold-font-base.css",
    )
    return any(marker in lowered for marker in markers)


def _html_looks_like_avature(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "avature.portal.id",
        ".avature.net/",
        "/jobapplication?",
        "/applicationmethods?",
        "/registrationmethods",
        "/careers/jobdetail/",
    )
    return any(marker in lowered for marker in markers)


def _html_looks_like_successfactors(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "successfactors.com",
        "jobs2web",
        "j2w.apply",
        "j2w.init",
        "rmkcdn.successfactors.com",
        "performancemanager4.successfactors.com",
    )
    return any(marker in lowered for marker in markers)


_STRONG_APPLY_URL_PATTERNS = (
    re.compile(
        r"""<meta[^>]+name=["']search-job(?:-mobile)?-apply-url["'][^>]+content=["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(r"""\bdata-apply-url=["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""["']applyUrl["']\s*:\s*["']([^"']+)["']""", re.IGNORECASE),
)
_GENERIC_APPLY_URL_PATTERNS = (
    re.compile(r"""\bhref=["']([^"']+)["']""", re.IGNORECASE),
)


def _extract_board_apply_urls(html: str, base_url: str, *, include_generic_hrefs: bool = True) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    patterns = _STRONG_APPLY_URL_PATTERNS
    if include_generic_hrefs:
        patterns += _GENERIC_APPLY_URL_PATTERNS
    for pattern in patterns:
        for match in pattern.finditer(html):
            raw = unescape(match.group(1)).replace("\\/", "/").strip()
            if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            resolved = urljoin(base_url, raw)
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def _url_uses_known_board_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    host_patterns = (
        *GEM_HOST_PATTERNS,
        *LEVER_HOST_PATTERNS,
        *WORKDAY_HOST_PATTERNS,
        *ASHBY_HOST_PATTERNS,
        *DOVER_HOST_PATTERNS,
        *ICIMS_HOST_PATTERNS,
        *AVATURE_HOST_PATTERNS,
        *WORKABLE_HOST_PATTERNS,
        *COMEET_HOST_PATTERNS,
        *BAMBOOHR_HOST_PATTERNS,
        *RIPPLING_HOST_PATTERNS,
        *UBER_HOST_PATTERNS,
        *MOTIONRECRUITMENT_HOST_PATTERNS,
        *REDUCTO_HOST_PATTERNS,
        *BYTEDANCE_HOST_PATTERNS,
        *LINKEDIN_HOST_PATTERNS,
    )
    return any(pattern in host for pattern in host_patterns)


def _direct_board_for_url(url: str, extraction_method: str = "", application_method: str = "") -> str | None:
    if application_method.casefold() == "email":
        return "email"

    extraction_method = extraction_method.casefold()
    if extraction_method == "greenhouse-api":
        return "greenhouse"

    host = (urlparse(url).hostname or "").casefold()
    if any(pattern in host for pattern in GEM_HOST_PATTERNS):
        return "gem"
    if any(pattern in host for pattern in LEVER_HOST_PATTERNS):
        return "lever"
    if any(pattern in host for pattern in WORKDAY_HOST_PATTERNS):
        return "workday"
    if any(pattern in host for pattern in DOVER_HOST_PATTERNS) or looks_like_dover_url(url):
        return "dover"
    if looks_like_oracle_hcm_url(url):
        raise ValueError(f"Oracle Cloud HCM is not yet supported as an autofill board: {url}")
    if any(pattern in host for pattern in AVATURE_HOST_PATTERNS) or looks_like_avature_url(url):
        return "avature"
    if any(pattern in host for pattern in ICIMS_HOST_PATTERNS) or looks_like_icims_url(url):
        return "icims"
    if any(pattern in host for pattern in ASHBY_HOST_PATTERNS) or looks_like_ashby_wrapper_url(url):
        return "ashby"
    if looks_like_phenom_url(url):
        return "phenom"
    if any(pattern in host for pattern in WORKABLE_HOST_PATTERNS):
        return "workable"
    if any(pattern in host for pattern in EIGHTFOLD_HOST_PATTERNS):
        return "eightfold"
    if any(pattern in host for pattern in SMARTRECRUITERS_HOST_PATTERNS):
        return "smartrecruiters"
    if any(pattern in host for pattern in COMEET_HOST_PATTERNS):
        return "comeet"
    if any(pattern in host for pattern in BAMBOOHR_HOST_PATTERNS):
        return "bamboohr"
    if any(pattern in host for pattern in RIPPLING_HOST_PATTERNS):
        return "rippling"
    if any(pattern in host for pattern in UBER_HOST_PATTERNS) and "/careers/" in url:
        return "uber"
    if any(pattern in host for pattern in MOTIONRECRUITMENT_HOST_PATTERNS):
        return "motionrecruitment"
    if any(pattern in host for pattern in REDUCTO_HOST_PATTERNS) and "/careers/" in url:
        return "reducto"
    if any(pattern in host for pattern in BYTEDANCE_HOST_PATTERNS) or looks_like_bytedance_url(url):
        return "bytedance"
    if any(h in host for h in LINKEDIN_HOST_PATTERNS) and "/jobs/view/" in url:
        return "linkedin"
    if looks_like_greenhouse_url(url):
        return "greenhouse"
    if looks_like_successfactors_url(url):
        return "successfactors"
    if looks_like_breezy_url(url):
        return "breezy"
    if looks_like_recruitee_url(url):
        return "recruitee"
    if looks_like_jobvite_url(url):
        return "jobvite"
    if looks_like_jazzhr_url(url):
        return "jazzhr"
    if looks_like_paycor_url(url):
        return "paycor"
    return None


def _resolved_board_and_url_for_submit(
    url: str,
    extraction_method: str = "",
    application_method: str = "",
) -> tuple[str, str]:
    direct_board = _direct_board_for_url(url, extraction_method, application_method)
    if direct_board is not None and _url_uses_known_board_host(url):
        return direct_board, url

    candidate_board: str | None = None
    candidate_url: str | None = None

    # Fallback: probe the JD page for downstream application links.
    # Wrapper pages can look like a supported board (for example Phenom) but
    # actually hand applicants off to another ATS such as Workday via an
    # embedded JSON applyUrl. Prefer that downstream target when present.
    try:
        from urllib.request import Request, urlopen

        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            final_url = resp.url if hasattr(resp, "url") else ""
            final_board = _direct_board_for_url(final_url, extraction_method, application_method) if final_url else None
            if final_board is not None and final_url and final_url != url and direct_board != "phenom":
                return final_board, final_url
            html = resp.read(500_000).decode("utf-8", errors="replace")
        html_lower = html.lower()
        allow_phenom_external_override = direct_board != "phenom" or bool(
            re.search(r'"externalapply"\s*:\s*true\b', html_lower)
        )
        for extracted_url in _extract_board_apply_urls(html, url, include_generic_hrefs=direct_board is None):
            extracted_board = _direct_board_for_url(extracted_url, extraction_method, application_method)
            if extracted_board is not None:
                if direct_board == "phenom" and not allow_phenom_external_override:
                    continue
                return extracted_board, extracted_url
        if looks_like_recruitee_wrapper_url(url) and html_looks_like_recruitee(html):
            candidate_board = "recruitee"
        elif _html_looks_like_avature(html):
            candidate_board = "avature"
        elif _html_looks_like_successfactors(html):
            candidate_board = "successfactors"
        elif "icims.com" in html or "talentbrew" in html_lower:
            candidate_board = "icims"
        elif "comeet" in html_lower:
            candidate_board = "comeet"
        elif "smartrecruiters.com" in html_lower:
            candidate_board = "smartrecruiters"
        elif "workable.com" in html_lower:
            candidate_board = "workable"
        elif "bamboohr.com" in html_lower:
            candidate_board = "bamboohr"
        elif _html_looks_like_eightfold(html):
            candidate_board = "eightfold"
        candidate_url = url
    except Exception:
        pass

    if candidate_board is not None:
        return candidate_board, candidate_url or url
    if direct_board is not None:
        return direct_board, url
    raise ValueError(f"Unsupported application board for URL: {url}")


def _board_for_url(url: str, extraction_method: str = "", application_method: str = "") -> str:
    board, _resolved_url = _resolved_board_and_url_for_submit(url, extraction_method, application_method)
    return board


def _persist_submit_resolution(out_dir: Path, meta: dict, *, original_url: str, resolved_url: str, board: str) -> None:
    if not resolved_url:
        return
    updated = False
    if resolved_url != str(meta.get("jd_source_resolved") or ""):
        meta["jd_source_resolved"] = resolved_url
        updated = True
    if not str(meta.get("board_url") or "").strip():
        meta["board_url"] = original_url or resolved_url
        updated = True
    if board and board != str(meta.get("board") or ""):
        meta["board"] = board
        updated = True
    if original_url and original_url != resolved_url and not str(meta.get("source_url") or "").strip():
        meta["source_url"] = original_url
        updated = True
    if not updated:
        return
    meta_path = out_dir / ".pipeline_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _script_for_board(board: str) -> Path:
    if board == "gem":
        return SCRIPT_DIR / "autofill_gem.py"
    if board == "lever":
        return SCRIPT_DIR / "autofill_lever.py"
    if board == "workday":
        return SCRIPT_DIR / "autofill_workday.py"
    if board == "dover":
        return SCRIPT_DIR / "autofill_dover.py"
    if board == "ashby":
        return SCRIPT_DIR / "autofill_ashby.py"
    if board == "greenhouse":
        return SCRIPT_DIR / "autofill_greenhouse.py"
    if board == "successfactors":
        return SCRIPT_DIR / "autofill_successfactors.py"
    if board == "breezy":
        return SCRIPT_DIR / "autofill_breezy.py"
    if board == "recruitee":
        return SCRIPT_DIR / "autofill_recruitee.py"
    if board == "jobvite":
        return SCRIPT_DIR / "autofill_jobvite.py"
    if board == "jazzhr":
        return SCRIPT_DIR / "autofill_jazzhr.py"
    if board == "paycor":
        return SCRIPT_DIR / "autofill_paycor.py"
    if board == "icims":
        return SCRIPT_DIR / "autofill_icims.py"
    if board == "avature":
        return SCRIPT_DIR / "autofill_avature.py"
    if board == "phenom":
        return SCRIPT_DIR / "autofill_phenom.py"
    if board == "eightfold":
        return SCRIPT_DIR / "autofill_eightfold.py"
    if board == "workable":
        return SCRIPT_DIR / "autofill_workable.py"
    if board == "smartrecruiters":
        return SCRIPT_DIR / "autofill_smartrecruiters.py"
    if board == "comeet":
        return SCRIPT_DIR / "autofill_comeet.py"
    if board == "bamboohr":
        return SCRIPT_DIR / "autofill_bamboohr.py"
    if board == "rippling":
        return SCRIPT_DIR / "autofill_rippling.py"
    if board == "uber":
        return SCRIPT_DIR / "autofill_uber.py"
    if board == "motionrecruitment":
        return SCRIPT_DIR / "autofill_motionrecruitment.py"
    if board == "reducto":
        return SCRIPT_DIR / "autofill_reducto.py"
    if board == "bytedance":
        return SCRIPT_DIR / "autofill_bytedance.py"
    if board == "email":
        return SCRIPT_DIR / "autofill_email.py"
    if board == "linkedin":
        return SCRIPT_DIR / "autofill_linkedin.py"
    raise ValueError(f"Unsupported board: {board}")


def _existing_submission_result(out_dir: Path) -> dict | None:
    migrate_role_output_layout(out_dir)
    fallback_payload: dict | None = None
    for submit_dir in submit_dirs_by_mtime(out_dir):
        payload = _read_submit_json(submit_dir / SUBMISSION_RESULT_JSON)
        if not isinstance(payload, dict):
            continue
        if payload.get("website_confirmed") is True:
            return payload
        if fallback_payload is None:
            fallback_payload = payload
    return fallback_payload


def _existing_notion_sync_status(out_dir: Path) -> dict | None:
    migrate_role_output_layout(out_dir)
    latest_payload: dict | None = None
    for submit_dir in submit_dirs_by_mtime(out_dir):
        payload = _read_submit_json(submit_dir / NOTION_SYNC_STATUS_JSON)
        if not isinstance(payload, dict):
            continue
        if latest_payload is None:
            latest_payload = payload
        if payload.get("status") == "synced":
            return payload
    return latest_payload


def _read_submit_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_notion_sync_module():
    script_path = SCRIPT_DIR / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("job_assets_notion_sync", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Notion sync helper from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coerce_utc_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resume_post_submit_sync(out_dir: Path) -> int:
    active_submit_dir = preferred_submit_dir_name_for_post_submit(out_dir)
    existing_status = _existing_notion_sync_status(out_dir) or {}
    retry_after_seconds = int(
        os.environ.get("NOTION_SYNC_PENDING_RECHECK_SECONDS", str(DEFAULT_PENDING_EMAIL_RECHECK_SECONDS))
    )
    if retry_after_seconds > 0 and existing_status.get("status") == "pending_email_confirmation":
        updated_at = _coerce_utc_datetime(existing_status.get("updated_at_utc"))
        if updated_at is not None:
            age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
            if age_seconds < retry_after_seconds:
                print(
                    "Submission already confirmed; the latest email check is still recent and remains pending. "
                    f"Rerun `python3 scripts/notion_sync.py {out_dir}` after the confirmation email arrives.",
                    file=sys.stderr,
                )
                return 0

    notion_sync = _load_notion_sync_module()
    original_submit_dir = os.environ.get(ACTIVE_SUBMIT_DIR_ENV)
    if active_submit_dir:
        os.environ[ACTIVE_SUBMIT_DIR_ENV] = active_submit_dir
    try:
        result = notion_sync.sync_application(
            out_dir,
            wait_for_email_seconds=int(os.environ.get("NOTION_SYNC_EMAIL_WAIT_SECONDS", "90")),
            allow_pending_email=True,
            fail_on_missing_token=False,
        )
    finally:
        if original_submit_dir is None:
            os.environ.pop(ACTIVE_SUBMIT_DIR_ENV, None)
        else:
            os.environ[ACTIVE_SUBMIT_DIR_ENV] = original_submit_dir
    status = str(result.get("status") or "")
    if status == "synced":
        destination = result.get("page_url") or result.get("page_id") or out_dir
        print(
            f"Submission already confirmed for {out_dir}; email confirmation and Notion sync are complete: {destination}",
            file=sys.stderr,
        )
        return 0
    if status == "pending_email_confirmation":
        print(
            "Submission already confirmed; Notion sync is still waiting for the confirmation email. "
            f"Rerun `python3 scripts/notion_sync.py {out_dir}` once the email arrives.",
            file=sys.stderr,
        )
        return 0
    if status == "missing_notion_token":
        print(
            "Submission already confirmed, but Notion sync is waiting on credentials. "
            f"Rerun `python3 scripts/notion_sync.py {out_dir}` after configuring NOTION_API_TOKEN.",
            file=sys.stderr,
        )
        return 0
    print(
        f"Submission already confirmed for {out_dir}; post-submit sync status: {status or 'unknown'}.",
        file=sys.stderr,
    )
    return 0


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _pending_user_input_path(out_dir: Path, submit_dirname: str) -> Path:
    return out_dir / submit_dirname / PENDING_USER_INPUT_JSON


def _normalized_known_board_name(board: str | None) -> str | None:
    normalized = str(board or "").strip().casefold()
    if not normalized or normalized == "unknown":
        return None
    return normalized


def _record_captcha_skip_result(out_dir: Path, submit_dirname: str, board: str) -> dict:
    payload = {
        "status": "skipped_captcha",
        "website_confirmed": False,
        "provider": board or "generic",
        "board": board or None,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": "Submission skipped: captcha required. Moving on to next job.",
    }
    submission_result_path = out_dir / submit_dirname / SUBMISSION_RESULT_JSON
    submission_result_path.parent.mkdir(parents=True, exist_ok=True)
    submission_result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _record_pending_user_input_result(
    out_dir: Path,
    submit_dirname: str,
    pending_payload: dict,
    *,
    board_hint: str | None = None,
) -> dict:
    questions = pending_payload.get("questions")
    if not isinstance(questions, list):
        questions = []
    board = _normalized_known_board_name(str(pending_payload.get("board") or ""))
    if board is None:
        board = _normalized_known_board_name(board_hint)
    payload = {
        "status": "pending_user_input",
        "website_confirmed": False,
        "provider": board or "generic",
        "board": board or None,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": "Submission paused because one or more answers require manual user input.",
        "questions": questions,
        "pending_user_input_path": str(_pending_user_input_path(out_dir, submit_dirname)),
    }
    submission_result_path = out_dir / submit_dirname / SUBMISSION_RESULT_JSON
    submission_result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _write_unsupported_board_log(out_dir: Path, meta: dict, reason: str, url: str) -> Path:
    unsupported_log = {
        "status": "unsupported_board",
        "url": url,
        "company": meta.get("company_proper") or meta.get("company"),
        "role": meta.get("role"),
        "reason": reason,
        "suggestion": "Apply manually using the generated resume and cover letter.",
        "resume": str(out_dir / "documents"),
        "logged_at_utc": datetime.now(UTC).isoformat(),
    }
    unsupported_path = role_submit_path(out_dir, "unsupported_board.json")
    unsupported_path.parent.mkdir(parents=True, exist_ok=True)
    unsupported_path.write_text(
        json.dumps(unsupported_log, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"Unsupported job board for {_display_path(out_dir)}: {reason} Logged to {_display_path(unsupported_path)}. "
        "Apply manually.",
        file=sys.stderr,
    )
    return unsupported_path


def _verify_submit_approved(output_dir: str | None) -> bool:
    """Check that the job has been explicitly approved for submission.

    Looks for evidence of approval: either the orchestrator set auto_submit
    in .pipeline_meta.json or the job DB status is 'submitting'/'submitted'.
    Returns True if submit is safe, False if we should fall back to draft.
    """
    if not output_dir:
        return True  # CLI direct runs are always intentional
    # Check for pipeline_meta approval marker
    meta_path = Path(output_dir) / ".pipeline_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # If the orchestrator set auto_submit, it's approved
            if meta.get("auto_submit"):
                return True
        except Exception:
            pass
    # If called from CLI directly (not through worker), allow it
    # Check if we're running inside a worker by looking for worker env
    if os.environ.get("JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR"):
        # Running inside worker — need approval evidence
        # Check the DB for the job status
        try:
            import sqlite3

            db_path = Path(output_dir).parent.parent.parent / "jobs.db"
            if not db_path.exists():
                db_path = Path.cwd() / "jobs.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT status FROM jobs WHERE output_dir = ?",
                    (str(output_dir),),
                ).fetchone()
                conn.close()
                if row and row["status"] in ("submitting", "submitted"):
                    return True
                if row:
                    print(
                        f"SAFETY: Job status is '{row['status']}', not 'submitting'. Blocking submit.", file=sys.stderr
                    )
                    return False
        except Exception:
            pass
    return True  # Default allow for CLI direct runs


def main() -> int:
    configure_runtime_trace(environ=os.environ, replace=True)
    parser = argparse.ArgumentParser(description="Dispatch application submit automation based on the job-board URL.")
    parser.add_argument(
        "target",
        help="Output directory (e.g. output/company/role-slug) or a job URL already present in .pipeline_meta.json.",
    )
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help="Only generate the board-specific autofill payload. Do not launch the browser automation.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the Playwright runtime in headless mode.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the application after autofill. Default behavior stops for review.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Fill the form and take screenshots but stop before submitting. Generates draft artifacts for review.",
    )
    parser.add_argument(
        "--reapply",
        action="store_true",
        help="Start a fresh submit attempt for this role in a new submit-* artifact directory.",
    )
    parser.add_argument(
        "--browser-provider",
        choices=("local", "steel"),
        default=None,
        help="Browser runtime to use for submit automation (default: env or local).",
    )
    parser.add_argument(
        "--provider",
        choices=VALID_PROVIDERS,
        default=None,
        help="Provider to use for generated application answers.",
    )
    args = parser.parse_args()

    out_dir = find_output_dir(args.target)
    migrate_role_output_layout(out_dir)
    submit_dirname = active_submit_dir_name(out_dir)
    if args.reapply:
        existing_active = submit_dirname if submit_dirname != "submit" else None
        new_submit_dir = ensure_reapply_submit_dir(out_dir)
        submit_dirname = new_submit_dir.name
        if existing_active == submit_dirname:
            print(
                f"Reusing in-progress reapply attempt in {_display_path(new_submit_dir)}",
                file=sys.stderr,
            )
        else:
            print(
                f"Starting fresh submit attempt in {_display_path(new_submit_dir)}",
                file=sys.stderr,
            )
    if args.reapply:
        existing_result = _read_submit_json((out_dir / submit_dirname) / SUBMISSION_RESULT_JSON)
    else:
        existing_result = _existing_submission_result(out_dir)
    if args.submit and existing_result and existing_result.get("website_confirmed") is True:
        notion_status = _existing_notion_sync_status(out_dir)
        if notion_status and notion_status.get("status") == "synced":
            print(
                f"Submission already confirmed for {out_dir}; skipping duplicate live resubmit.",
                file=sys.stderr,
            )
            return 0
        return _resume_post_submit_sync(out_dir)

    meta = load_meta(out_dir)
    jd_url = preferred_meta_job_url(meta, keys=("board_url", "jd_source", "jd_source_resolved"))
    try:
        board, resolved_submit_url = _resolved_board_and_url_for_submit(
            jd_url,
            extraction_method=str(meta.get("jd_extraction_method") or ""),
            application_method=str(meta.get("application_method") or ""),
        )
    except ValueError:
        try:
            board = _board_for_url(
                jd_url,
                extraction_method=str(meta.get("jd_extraction_method") or ""),
                application_method=str(meta.get("application_method") or ""),
            )
            resolved_submit_url = jd_url
        except ValueError:
            _write_unsupported_board_log(
                out_dir,
                meta,
                f"No autofill support for this job board URL: {jd_url}",
                jd_url,
            )
            return 0
    _persist_submit_resolution(out_dir, meta, original_url=jd_url, resolved_url=resolved_submit_url, board=board)
    script = _script_for_board(board)
    if not script.exists():
        _write_unsupported_board_log(
            out_dir,
            meta,
            f"No autofill implementation available for detected board '{board}'.",
            resolved_submit_url,
        )
        return 0

    # Defense-in-depth: verify the job was explicitly approved before submitting
    submit = args.submit and not args.draft
    if submit and not _verify_submit_approved(str(out_dir)):
        print("SAFETY: --submit passed but job was not explicitly approved. Falling back to --draft.", file=sys.stderr)
        submit = False
    if submit:
        trace_metadata = {"surface": "submit_application", "board": board, "output_dir": str(out_dir)}
        ensure_action_allowed(
            "live_submit",
            explicit_approval=True,
            metadata=trace_metadata,
            environ=os.environ,
        )
        emit_trace(
            "live_submit_started",
            action="live_submit",
            metadata=trace_metadata,
            environ=os.environ,
        )

    cmd = python_script_command(script, str(out_dir), environ=os.environ)
    if args.payload_only:
        cmd.append("--payload-only")
    if args.headless:
        cmd.append("--headless")
    if submit:
        cmd.append("--submit")
    # Draft is the default for all board scripts (no --submit = draft).
    # Don't pass --draft explicitly — future board scripts may not accept it.
    if args.browser_provider:
        cmd.extend(["--browser-provider", args.browser_provider])
    if args.provider:
        cmd.extend(["--provider", args.provider])

    env = os.environ.copy()
    env[ACTIVE_SUBMIT_DIR_ENV] = submit_dirname
    _clear_stale_current_attempt_terminal_artifacts(out_dir, submit_dirname)
    started_at_utc = datetime.now(UTC)
    completed: subprocess.CompletedProcess = run_worker_subprocess(cmd, cwd=PROJECT_ROOT, env=env)
    if submit:
        emit_trace(
            "live_submit_completed",
            action="live_submit",
            status="ok" if completed.returncode == 0 else "error",
            metadata={
                "surface": "submit_application",
                "board": board,
                "output_dir": str(out_dir),
                "returncode": completed.returncode,
            },
            environ=os.environ,
        )
    current_result = _read_submit_json(out_dir / submit_dirname / SUBMISSION_RESULT_JSON)
    if current_result is not None:
        current_status = str(current_result.get("status") or "").strip().casefold()
        if current_status == "skipped_captcha":
            if not submit:
                _finalize_non_submit_attempt(out_dir, submit_dirname, meta)
            print(
                str(current_result.get("message") or "Submission skipped: captcha required. Moving on.").strip(),
                file=sys.stderr,
            )
            return 0
        if current_status in {
            "pending_user_input",
            "needs_manual",
            "skipped_auth",
            "skipped_auth_failure",
            "auth_failed",
            "auth_unknown",
            "auth_guarded",
            "service_unavailable",
            "job_closed",
            "not_easy_apply",
            "already_applied",
            "unknown",
        }:
            if not submit:
                _finalize_non_submit_attempt(out_dir, submit_dirname, meta)
            print(
                str(current_result.get("message") or f"Submission stopped with status: {current_status}.").strip(),
                file=sys.stderr,
            )
            return 0
    if completed.returncode == CAPTCHA_SKIP_EXIT_CODE:
        _record_captcha_skip_result(out_dir, submit_dirname, board)
        if not submit:
            _finalize_non_submit_attempt(out_dir, submit_dirname, meta)
        print(
            f"Submission skipped for {_display_path(out_dir)}: captcha required. Moving on.",
            file=sys.stderr,
        )
        return 0
    pending = load_pending_user_input_for_submit_attempt(
        out_dir,
        submit_dirname=submit_dirname,
        started_at_utc=started_at_utc,
    )
    if pending is not None:
        pending_path, pending_payload = pending
        submission_result_path = out_dir / submit_dirname / SUBMISSION_RESULT_JSON
        if not submission_result_path.exists():
            _record_pending_user_input_result(out_dir, submit_dirname, pending_payload, board_hint=board)
        if not submit:
            _finalize_non_submit_attempt(out_dir, submit_dirname, meta)
        print(
            "Submission needs manual answers; logged them to "
            f"{_display_path(pending_path)}. Skipping this application for now.",
            file=sys.stderr,
        )
        return 0
    if completed.returncode != 0:
        return completed.returncode
    if not submit:
        _finalize_non_submit_attempt(out_dir, submit_dirname, meta)
        from pipeline_audit_loop import audit_draft_outcome, clear_audit_failure_report

        draft_audit = audit_draft_outcome(out_dir, board_name=board)
        if draft_audit.kind == "ready":
            clear_audit_failure_report(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
