# File Naming & Output Structure

All output files follow this structure:

```
output/<company>/<role-slug>/
  .pipeline_meta.json
  answer_refresh_status.json                 # Durable answer-refresh proof state
  answer_verification_status.json            # Durable answer-verification proof state
  draft_summary.md                           # Editable draft review summary
  draft_summary.original.md                  # Immutable summary baseline for diffing
  draft_summary.png                          # Rendered summary image
  draft_status.json                          # Draft review lifecycle state
  draft_overrides.json                       # User-applied answer overrides
  content/
    jd_raw.md
    jd_parsed.json
    ranked_bullets.json
    resume_content_draft.json
    resume_content.json
    cover_letter_text.txt
  documents/
    <Candidate Name> Resume - <Company>.docx
    <Candidate Name> Resume - <Company>.pdf
    <Candidate Name> Cover Letter - <Company>.docx
    <Candidate Name> Cover Letter - <Company>.pdf
    <Candidate Name> Cover Letter - <Company>.txt
  submit/
    <board>_autofill_payload.json
    <board>_autofill_report.md
    <board>_autofill_report.json
    <board>_autofill_pages/page_XX.png
    application_answers.json
    application_answers_raw.txt
    application_answers_fallback_raw.txt
    answer_verification.json
    answer_verification_raw.txt
    application_submission_result.json
    application_confirmation_website.json
    application_confirmation_email.json
    confirmation_email_reply.json
    notion_sync_status.json
  interview_prep/                              # Optional, on-demand
    interview_prep.md                          # Full prep guide (markdown)
    <Candidate Name> Interview Prep - <Company>.docx
    <Candidate Name> Interview Prep - <Company>.pdf
    .progress.json                             # Generation progress (transient)
    .generating                                # Lock file with PID (transient)
```

- `<company>` — lowercase slug of the company name (e.g., `samsara`, `scale-ai`)
- `<role-slug>` — lowercase, hyphenated slug of the role title (e.g., `agent-platform-pm`, `senior-pm-iot`, `staff-product-manager`)
- `<Company>` in filenames — proper case as it appears in the JD (e.g., `Samsara`, `Scale AI`)

**The role subdirectory ensures multiple applications to the same company don't overwrite each other.**

If the candidate is only applying to one role at a company, the role subdirectory is still required for consistency.

For explicit reapply flows, the same submit artifacts live under a per-attempt `submit-*` directory instead of the default `submit/` bucket. Post-submit state such as `confirmation_email_reply.json` follows the confirmed submit attempt so reruns do not overwrite or suppress a later reapply.

`answer_refresh_status.json` lives at the role output root so every runtime surface can read the same explicit refresh state without a database lookup. Current states are:

- `unknown` - legacy draft that predates the answer-refresh proof contract
- `pending` - an explicit answer-affecting regenerate request is waiting for fresh proof
- `fresh` - current answer artifacts were rewritten for the active request and include answer provider/time metadata
- `not_applicable` - the current draft had no generated application answers to refresh
- `failed` - a requested refresh did not produce fresh proof and should not be treated as a silent success

Explicit answer-affecting regenerations, including full regenerate and restart-pipeline, bypass reusable answer caches, rewrite `submit/application_answers.json` plus raw answer artifacts, and then finalize `answer_refresh_status.json` from the rewritten artifacts.

`answer_verification_status.json` also lives at the role output root so CLI, TUI, web, and repair flows can read one shared verifier state without a database lookup. Current states are:

- `unknown` - legacy draft that predates the answer-verification proof contract
- `pending` - the current draft is waiting for verifier proof
- `verified` - non-deterministic generated answers passed the verifier for the active submit attempt
- `not_applicable` - the current draft had no non-deterministic generated answers to verify
- `blocked` - the verifier found one or more answers that require explicit user review or input
- `failed` - verifier execution failed before current-attempt proof was recorded

The active submit attempt also records `submit/answer_verification.json` with per-question verifier verdicts and may record `submit/answer_verification_raw.txt` for provider-specific debugging.

For LinkedIn Easy Apply failures that stop before Review, the current attempt may intentionally have:

- `submit/application_submission_result.json` with `status: "failed"` and a LinkedIn-specific `failure_type`
- `submit/linkedin_submit_debug.png`
- `submit/linkedin_autofill_pages/*.png`

without a fresh `submit/linkedin_autofill_report.{json,md}` or `submit/linkedin_autofill_pre_submit.png`. In that state, the debug screenshot and failed submission result are the source of truth for the current attempt.
