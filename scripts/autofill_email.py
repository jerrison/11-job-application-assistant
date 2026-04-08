#!/usr/bin/env python3
"""Generate and send an email-based job application via gws CLI."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (  # noqa: E402
    APPLICATION_PROFILE_PATH,
    MASTER_RESUME_PATH,
    find_cover_letter_text,
    find_resume_file,
    json_dumps_pretty,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    reply_to_confirmation_email,
    sync_notion_after_submit,
)
from output_layout import migrate_role_output_layout, role_submit_path  # noqa: E402
from project_env import load_project_env  # noqa: E402

AUTOFILL_PAYLOAD_JSON = "email_autofill_payload.json"
SUBMISSION_RESPONSE_JSON = "email_submission_response.json"
SENT_EMAIL_EML = "email_application_sent.eml"
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"


load_project_env()


def _compose_email_body(
    *,
    profile,
    application_profile,
    cover_letter_text: str,
    role_title: str,
    company: str,
) -> str:
    """Compose a professional application email from cover letter text."""
    # Strip greeting/signoff from cover letter if present — we'll add our own
    lines = cover_letter_text.strip().splitlines()
    body_lines: list[str] = []
    skip_signoff = False
    for line in lines:
        stripped = line.strip().casefold()
        # Skip common letter greetings
        if stripped.startswith(("dear ", "to whom it may concern", "hi ", "hello ")):
            continue
        # Skip signoff block
        if any(
            stripped.startswith(prefix)
            for prefix in (
                "best regards",
                "regards",
                "sincerely",
                "thank you for",
                "warm regards",
                "kind regards",
                "best,",
                "thanks,",
            )
        ):
            skip_signoff = True
        if skip_signoff:
            continue
        body_lines.append(line)

    cover_body = "\n".join(body_lines).strip()

    location = application_profile.location or profile.location or ""
    linkedin = application_profile.linkedin or profile.linkedin or ""

    parts = [
        f"Dear {company} Hiring Team,\n",
        f"I'm writing to express my strong interest in the {role_title} position at {company}.\n",
        cover_body,
        "\nFor your reference:",
        f"- Name: {profile.full_name}",
        f"- Email: {profile.email}",
    ]
    if location:
        parts.append(f"- Location: {location}")
    if linkedin:
        parts.append(f"- LinkedIn: {linkedin}")
    parts.append(
        "\nI've attached my resume for your review. I'd welcome the opportunity "
        "to discuss how my experience aligns with what you're looking for.\n"
    )
    parts.append(f"Best regards,\n{profile.full_name}")
    return "\n".join(parts)


def _build_mime_message(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    resume_path: Path,
) -> MIMEMultipart:
    """Build a MIME message with text body and resume attachment."""
    msg = MIMEMultipart()
    msg["To"] = to_email
    msg["From"] = from_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain", "utf-8"))

    attachment = MIMEBase("application", "pdf")
    attachment.set_payload(resume_path.read_bytes())
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        f'attachment; filename="{resume_path.name}"',
    )
    msg.attach(attachment)
    return msg


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)

    application_email = str(meta.get("application_email") or "").strip()
    if not application_email:
        raise ValueError(
            "Email-based submission requires 'application_email' in .pipeline_meta.json. "
            "Add it manually or re-run the pipeline."
        )

    email_subject = str(meta.get("application_email_subject") or meta.get("jd_title") or "").strip()
    if not email_subject:
        raise ValueError("Could not determine email subject — set 'application_email_subject' in .pipeline_meta.json.")

    profile = parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    resume_path = find_resume_file(out_dir)
    cover_letter_text = find_cover_letter_text(out_dir)

    company = str(meta.get("company_proper") or meta.get("company") or "").strip()
    role_title = email_subject

    email_body = _compose_email_body(
        profile=profile,
        application_profile=application_profile,
        cover_letter_text=cover_letter_text,
        role_title=role_title,
        company=company,
    )

    payload = {
        "board": "email",
        "provider": provider,
        "out_dir": str(out_dir),
        "job_url": str(meta.get("jd_source_resolved") or meta.get("jd_source") or ""),
        "company": company,
        "role_title": role_title,
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "to_email": application_email,
        "subject": email_subject,
        "body": email_body,
        "resume_path": str(resume_path),
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, AUTOFILL_PAYLOAD_JSON)),
            "submission_response": str(role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON)),
            "sent_email_eml": str(role_submit_path(out_dir, SENT_EMAIL_EML)),
            "website_confirmation_json": str(role_submit_path(out_dir, WEBSITE_CONFIRMATION_JSON)),
            "email_confirmation_json": str(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON)),
            "notion_sync_status_json": str(role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON)),
        },
    }
    return payload


def _send_email(payload: dict) -> dict:
    """Send the application email via gws CLI."""
    if not shutil.which("gws"):
        raise RuntimeError("gws CLI is not installed. Install from https://github.com/googleworkspace/cli")

    resume_path = Path(payload["resume_path"])
    if not resume_path.exists():
        raise FileNotFoundError(f"Resume file not found: {resume_path}")

    msg = _build_mime_message(
        from_email=payload["candidate_email"],
        to_email=payload["to_email"],
        subject=payload["subject"],
        body=payload["body"],
        resume_path=resume_path,
    )

    # Save EML for records
    eml_path = Path(payload["artifacts"]["sent_email_eml"])
    eml_path.parent.mkdir(parents=True, exist_ok=True)
    eml_path.write_bytes(msg.as_bytes())

    # Base64url encode for Gmail API
    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    result = subprocess.run(
        [
            "gws",
            "gmail",
            "users",
            "messages",
            "send",
            "--params",
            '{"userId": "me"}',
            "--json",
            json.dumps({"raw": raw_b64}),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    response: dict = {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }

    if result.returncode == 0:
        try:
            response["json"] = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    return response


def _run_submit(payload_path: Path, headless: bool, submit: bool) -> int:
    """Email-based submission — headless/browser flags are ignored."""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])

    print(f"Email application for {payload['role_title']} at {payload['company']}")
    print(f"  To: {payload['to_email']}")
    print(f"  Subject: {payload['subject']}")
    print(f"  Resume: {payload['resume_path']}")

    if not submit:
        print("\nEmail composed but not sent (pass --submit to send).")
        print(f"Preview the email body in: {payload_path}")
        return 0

    submitted_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
    response = _send_email(payload)

    response_path = role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON)
    response_path.write_text(json_dumps_pretty(response) + "\n", encoding="utf-8")

    if response["returncode"] != 0:
        error_msg = response.get("stderr") or response.get("stdout") or "Unknown error"
        raise RuntimeError(f"Email send failed (exit {response['returncode']}). {error_msg}\nSee {response_path}")

    message_id = ""
    if "json" in response:
        message_id = str(response["json"].get("id") or "")

    outcome = {
        "status": "confirmed",
        "reason": "email_sent",
        "snapshot": {
            "url": payload["job_url"],
            "page_text": f"Application email sent to {payload['to_email']}. Gmail message ID: {message_id}",
        },
        "errors": [],
        "invalid_fields": [],
    }

    # For email-based submissions, the sent email IS the confirmation —
    # pass a synthetic email_confirmation so Notion sync doesn't wait for
    # a reply that won't come.
    email_confirmation = {
        "subject": f"Application: {payload['subject']}",
        "date": submitted_at_utc,
        "from": payload["candidate_email"],
        "to": payload["to_email"],
        "message_id": message_id,
    }

    sync_result = sync_notion_after_submit(
        payload,
        outcome,
        provider="email",
        email_confirmation=email_confirmation,
        min_received_at_utc=submitted_at_utc,
    )
    reply_to_confirmation_email(payload, board_name="email", email_confirmation=email_confirmation)
    print(f"Email application sent successfully to {payload['to_email']}.")
    status = str(sync_result.get("status") or "")
    if status:
        print(f"Notion sync status: {status}")
    return 0


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name="email",
        build_payload_fn=_build_payload,
        has_browser=True,
        run_browser_fn=_run_submit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
