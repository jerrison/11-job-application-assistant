---
title: "Use Gmail RFC822 upload fallback for oversized confirmation email replies"
category: integration-issues
date: 2026-03-26
tags:
  - confirmation-email
  - gmail
  - rfc822-upload
  - cli-size-limit
  - submit-dir
  - stale-reply-state
component: email_processing
components:
  - scripts/application_submit_common.py
  - tests/test_submit_application.py
problem_type: integration_issue
root_cause: wrong_api
resolution_type: code_fix
severity: medium
---

# Gmail Upload Fallback For Oversized Confirmation Email Replies

## Problem

Confirmation-email self-replies could fail even when the confirmation thread, report, screenshot, and PDFs were all present. The shared helper always tried Gmail's inline `gws ... messages send --json` path, and larger applications could exceed the CLI payload limit and stop with `reason=email_payload_too_large`.

## Symptoms

- `send_confirmation_email_reply(...)` returned `status=not_sent` with `reason=email_payload_too_large`
- The employer confirmation thread existed in Gmail, but no self-reply appeared until manual recovery
- The failure showed up on a real Skydio application where the screenshot plus PDF attachments pushed the serialized message over the inline send limit
- Reply-state artifacts could retain stale `last_reason` and `last_error` values after a later successful recovery

## What Didn't Work

- Treating oversized replies as a terminal `not_sent` outcome was wrong; the message itself was valid, only the transport path was wrong
- Manual recovery through Gmail upload proved the reply could be sent, but that did not fix the shared workflow
- Writing the recovery `.eml` file outside the current working directory failed because `gws --upload` rejected paths outside `cwd`

## Solution

Keep the existing inline send path for smaller replies, but automatically fall back to Gmail's RFC822 upload path when the serialized JSON body becomes too large.

```python
GMAIL_INLINE_SEND_MAX_JSON_BYTES = 900_000


def _send_confirmation_email_message(
    msg: Message,
    *,
    thread_id: str,
    submit_dir: Path,
) -> subprocess.CompletedProcess[str]:
    msg_bytes = msg.as_bytes()
    body_json = json.dumps(
        {
            "raw": urlsafe_b64encode(msg_bytes).decode("ascii"),
            "threadId": thread_id,
        }
    )
    if len(body_json) > GMAIL_INLINE_SEND_MAX_JSON_BYTES:
        return _send_confirmation_email_message_upload(
            msg_bytes,
            thread_id=thread_id,
            submit_dir=submit_dir,
        )
    return subprocess.run([... "--json", body_json], ...)
```

The upload helper writes a temporary RFC822 file inside the submit directory, sends it with Gmail's upload API, and deletes the file in a `finally` block:

```python
def _send_confirmation_email_message_upload(
    msg_bytes: bytes,
    *,
    thread_id: str,
    submit_dir: Path,
) -> subprocess.CompletedProcess[str]:
    upload_path = submit_dir / f".confirmation_email_reply_{os.getpid()}_{time.time_ns()}.eml"
    upload_path.write_bytes(msg_bytes)
    try:
        return subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                json.dumps({"userId": "me"}),
                "--json",
                json.dumps({"threadId": thread_id}),
                "--upload",
                upload_path.name,
                "--upload-content-type",
                "message/rfc822",
            ],
            cwd=submit_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        upload_path.unlink(missing_ok=True)
```

The reply-state writer also now clears stale failure metadata when a later write succeeds cleanly:

```python
if reason is not None:
    state["last_reason"] = reason
else:
    state.pop("last_reason", None)

if error:
    state["last_error"] = error
else:
    state.pop("last_error", None)
```

Regression coverage was added in `test_send_confirmation_email_reply_uses_upload_when_inline_payload_is_too_large`, which verifies the upload fallback, temp-file cleanup, and stale-state cleanup.

## Why This Works

The original message composition was valid; the failure came from forcing every reply through the inline JSON transport. The size gate keeps the fast path for normal cases and switches to the correct transport for large MIME payloads.

Writing the `.eml` file inside the submit directory matters because `gws --upload` validates that the upload target resolves under the current working directory. Using `cwd=submit_dir` plus a relative filename makes the fallback work reliably in the same artifact bucket that already owns the reply state.

Clearing stale `last_reason` and `last_error` ensures the reply-state artifact describes the current outcome instead of preserving an obsolete failure after a later successful retry.

## Prevention

- Keep the inline Gmail path as the default, but guard it with a hard size threshold and fall back automatically instead of returning `not_sent`
- When using `gws --upload`, create the upload file inside the working directory and pass a relative filename
- Treat reply-state failure fields as transient metadata; clear them on later success so operators do not debug solved failures
- Keep regression tests for:
  - oversized inline payloads routing to the upload path
  - temp `.eml` cleanup after send
  - successful upload sends recording `sent=true`
  - stale `last_reason` and `last_error` being removed on success

## Investigation Steps

1. Confirmed a real Skydio confirmation thread existed, but the automatic reply state was `not_sent` with `reason=email_payload_too_large`
2. Re-ran the reply manually and verified the helper failed before Gmail accepted the message
3. Proved the same message could be sent via Gmail's RFC822 upload path
4. Hit `gws` path validation when the temporary `.eml` lived outside the current working directory, then confirmed that sending from the submit directory fixed that constraint
5. Codified the upload fallback and added a regression test for the oversized payload case

## Cross-References

- Related doc with moderate overlap: `docs/solutions/logic-errors/submit-attempt-scoped-confirmation-email-replies.md`
- GitHub issues: no related issues found via `gh issue list --search "confirmation email payload large gmail upload gws" --state all --limit 5`
