# Job Application Assistant

This repository is the code-only application repo for the job application automation system. It intentionally excludes user-owned runtime state such as resumes, context files, databases, generated outputs, and browser profiles.

Until the runtime-home migration is complete, local development may still create or require local `master_resume.md`, `work_stories.md`, `candidate_context.md`, `application_profile.md`, `jobs.db`, and `output/` beside the checkout. Those files are treated as runtime inputs and stay ignored here.

The repo supports non-interactive asset generation with Claude, GPT/Codex, Gemini, or the direct OpenAI API. You do not need to open an interactive CLI first.

## Install the CLI

The preferred interface is the `job-assets` command. Install it into your `PATH` and install the man pages:

```bash
python3 bin/job-assets install
```

This installs:
- `job-assets`
- `job-assets-codex`
- `job-assets-claude`

After installation, use:

```bash
job-assets --help
man job-assets
man job-assets-codex
```

## Single job

Run the full single-job workflow with the default provider:

```bash
job-assets "https://boards.greenhouse.io/company/jobs/12345"
job-assets --submit "https://boards.greenhouse.io/company/jobs/12345"
```

Use an explicit provider:

```bash
job-assets --provider claude "https://boards.greenhouse.io/company/jobs/12345"
job-assets --provider codex "https://boards.greenhouse.io/company/jobs/12345"
```

Generate only the tailored resume and cover letter:

```bash
job-assets apply "https://boards.greenhouse.io/company/jobs/12345"
```

Use provider-specific aliases:

```bash
job-assets-claude "https://boards.greenhouse.io/company/jobs/12345"
job-assets-codex "https://boards.greenhouse.io/company/jobs/12345"
job-assets-codex --submit "https://boards.greenhouse.io/company/jobs/12345"
```

URL-based runs now fail fast unless the website extraction step yields a usable JD. The scraper will retry alternate extraction paths before stopping, including same-site JD discovery when the first page is a thinner application shell, rather than generating assets from weak or shell-page content.

Each new asset run also refreshes `work_stories.md` and `candidate_context.md` from their source Google Docs unless you pass `--skip-sync`. Those syncs now run in parallel before content generation begins.

Bare `job-assets <jd_source>` now defaults to the end-to-end single-job pipeline. It generates the tailored resume and cover letter, then continues into supported board autofill and stops at review by default unless you add `--submit`. `job-assets apply ...` is the asset-only variant, and `job-assets pipeline ...` remains available as the explicit equivalent of the bare command.

When you rerun the same single-job pipeline after the deterministic draft has not changed, the CLI now reuses the existing `content/` files instead of calling the provider again. If the finalized content hashes also still match, it reuses the existing `documents/` assets instead of rebuilding unchanged resume and cover-letter files.

Application-form defaults live in `application_profile.md`, including LinkedIn, GitHub, website, work-authorization, compensation/salary-comfort, undergraduate GPA, and self-ID defaults. Edit that file directly, or use:

```bash
job-assets profile show
job-assets profile path
job-assets submit output/company/role-slug
job-assets notion-sync output/company/role-slug
```

Resume builds keep `San Francisco, CA` in the contact line when any supported job location is in California. For roles outside California, the resume header starts with `jerrisonli@gmail.com`.

If local extraction still fails, the scraper can optionally fall back to Cloudflare Browser Rendering's crawl endpoint. Set:

```bash
export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_BROWSER_RENDERING_API_TOKEN=...
```

`CLOUDFLARE_API_TOKEN` also works as the token variable. The Cloudflare fallback is only used after `scrapling` and the structured HTML fallback both fail.

The resume page break is now finalized after the bullets and summary are chosen. The pipeline auto-balances `page_break_before` to avoid an unnecessarily sparse first page when possible, without changing the selected bullet mix.

## Batch generation

Standard batch flow:

```bash
job-assets batch --provider claude
job-assets batch --provider codex --max-parallel 6
```

Advanced parallel flow:

```bash
job-assets parallel --provider claude
job-assets parallel --provider codex --max-parallel 6
job-assets-codex parallel --max-parallel 6
job-assets-claude parallel --dry-run
```

`job-assets batch` and `job-assets parallel` now share the same high-throughput worker pool. Research and drafting run concurrently across jobs, while the build step stays sequential because LibreOffice PDF conversion cannot run safely in parallel. The default worker count auto-sizes to `max(4, min(cpu_count * 2, 16))` unless `--max-parallel`, `JOB_ASSETS_MAX_PARALLEL`, or `MAX_PARALLEL` overrides it. Same-company workers coordinate through a lock on `output/<company>/research_cache.json` so one company is researched once even when several roles are fanned out together. On macOS, the PDF builders force LibreOffice onto the `svp` VCL backend during headless conversion and override any inherited `SAL_USE_VCLPLUGIN` value such as `osx`, which can otherwise trigger AppKit/menu-bar crashes even under `--headless`.

## Provider execution parity

The CLI is tuned to mirror Codex and Claude Code TUI behavior as closely as repo automation allows. Claude runs default to `claude-sonnet-4-6` with `--permission-mode auto`, and Codex runs default to `gpt-5.4` with `model_reasoning_effort="xhigh"`.

When `CODEX_PROFILE` is set and the explicit `CODEX_MODEL`, `CODEX_REASONING_EFFORT`, `CODEX_APPROVAL_POLICY`, and `CODEX_SANDBOX_MODE` overrides are unset, the CLI now defers those core execution settings to the Codex profile just like the interactive TUI. Non-interactive Codex runs now launch under a temporary `CODEX_HOME` that preserves auth plus sanitized config but strips user `mcp_servers`, so repo automation does not pay startup and token costs for unrelated desktop MCP servers. Use `CLAUDE_EXTRA_ARGS` and `CODEX_EXTRA_ARGS` only when you intentionally want repo runs to add provider-specific flags beyond the shared defaults.

Shared provider argv plus mode-specific search and tool policy now live in `scripts/llm_provider.py`. That keeps asset generation, fix passes, and submit-time answer generation aligned across all supported job boards instead of letting individual submitters drift onto custom provider behavior.

## GitHub automation

Pushes to `codex/*` branches now open or reuse a GitHub pull request targeting `main` and ask GitHub to auto-merge it with the workflow's configured merge method. `unit-tests` now run on both `codex/*` pushes and `main` pull requests so workflow-created PRs can still satisfy branch protection.

The repository-level Actions setting that allows GitHub Actions to create pull requests must stay enabled for that workflow to work.

This repo does not rely on local branch switching to land Codex work. That matters because generated `output/` artifacts often leave the local worktree dirty during application runs.

Durable repo changes should always follow these rules:
- Ensure the change works across all supported job boards, or document clearly why it is board-specific.
- If a live application run exposes a bug or workflow gap, fix it in that stream when feasible rather than just logging it for later, and generalize the fix across the supported boards instead of letting one board drift ahead.
- Update `AGENTS.md`, regenerate all provider copies with `uv run python scripts/sync_agent_files.py`, and refresh any relevant workflow docs such as this `README.md` when durable behavior changes and new learnings should persist.
- Track every artifact that is part of the repo's intended history, and ignore recurring transient artifacts so the worktree does not stay dirty for avoidable reasons.
- Commit the change, push the `codex/*` branch, let the PR workflow run, and merge the GitHub PR into `main`.

## Board submit automation

If you already have assets for a supported public application board, you can generate an autofill payload and launch a reviewable browser flow:

```bash
job-assets submit output/figma/pm-design-tools
```

If you want to go straight from a JD source into this submit flow in one command, use bare `job-assets <jd_source>` (or explicit `job-assets pipeline ...`) instead of running `apply` and `submit` separately.

Supported boards today:
- Greenhouse
- Gem
- Lever
- Ashby
- Dover

This uses the existing resume PDF plus cover letter text in that output directory, fills the public application, and stops before submit by default. To only generate the payload:

```bash
job-assets submit output/company/role-slug --payload-only
```

To submit after review:

```bash
job-assets submit output/company/role-slug --submit
```

To explicitly reapply for an already-submitted role and preserve the old submit artifacts:

```bash
job-assets submit output/company/role-slug --submit --reapply
job-assets pipeline --submit --reapply "https://job-posting.example.com/role"
```

To use Steel as the browser backend instead of the local Playwright browser:

```bash
job-assets submit output/company/role-slug --submit --browser-provider steel
```

`--browser-provider steel` (or `JOB_ASSETS_BROWSER_PROVIDER=steel`) creates a Steel-backed remote Chromium session. In Steel Cloud mode it requires `STEEL_API_KEY`; for self-hosted Steel use `STEEL_LOCAL=true` and optionally `STEEL_BASE_URL`. When a manual CAPTCHA or review step is required, the submitter prints the Steel session viewer URL so you can continue in your own browser.

The autofill flow uses `application_profile.md` for work authorization, sponsorship, salary-comfort defaults, free-text compensation expectations, undergraduate GPA, text-message consent, pronouns, LinkedIn, GitHub, website, "How did you hear about us?" answers, voluntary self-identification answers, and the Gmail address to use for verification/security codes. A shared positive-fit policy now answers discrete fit-screening prompts affirmatively across supported boards and widget types, including hybrid/in-office/commute questions, relocation and travel willingness, product usage, and general experience/background confirmations. Degree/license/certification claims only answer `Yes` when the credential is explicitly supported by `application_profile.md` or `master_resume.md`. Open-ended questions are still generated fresh from the tailored assets, company research, and candidate context.

If a board asks the specific onsite proceed/start/location question pattern, the submitter now answers it deterministically as `Yes`, the Monday in two weeks in long-date format such as `March 23, 2026`, and the configured city from `application_profile.md` such as `San Francisco`. If a long free-text question asks for specialized domain detail the automation cannot safely infer, the run now writes `submit/pending_user_input.json` and stops before submission instead of guessing or collapsing the answer to a yes/no default. The same fail-closed path now applies when any autofill field remains planned but unconfirmed on the live form: the run writes those unresolved fields into `submit/pending_user_input.json` and will not submit until every planned field is confirmed.

LinkedIn Easy Apply has one extra resume rule: LinkedIn-sourced jobs should still generate employer-named resume files (`Jerrison Li Resume - Asurion.pdf`, not `... - LinkedIn.pdf`). When the Easy Apply modal exposes a visible upload/change resume control, the submitter now re-uploads that current employer-named file and only treats it as verified when the live UI shows it as the selected attachment. If LinkedIn reaches Review without exposing any visible upload/change path, the draft may continue, but the report will label the outcome as `review_without_visible_resume_controls` rather than claiming a verified fresh upload.

Per-role output is now grouped by function:
- `output/<company>/<role>/content/` for JD parse output and LLM-editable source files such as `resume_content.json` and `cover_letter_text.txt`
- `output/<company>/<role>/documents/` for final resume and cover-letter `.docx` / `.pdf` assets
- `output/<company>/<role>/submit/` for board autofill payloads, screenshots, debug HTML, generated application answers, and confirmation / Notion-sync artifacts

Transient provider transcripts and one-off debug artifacts should be ignored rather than left as recurring untracked files.

When you reapply explicitly with `--reapply`, the submitter creates one fresh sibling directory such as `submit-20260313T171500Z/` for that reapply and keeps reusing it across retries until the submission actually completes. After website confirmation, the active pointer resets so a later explicit reapply gets its own new `submit-*` directory. Matching cached artifacts from earlier attempts such as `application_answers.json` or Greenhouse `greenhouse_application_page.html` are still reused when the live inputs have not changed.

Each autofill run also writes board-specific artifacts such as:
- `submit/greenhouse_autofill_report.md` and `submit/greenhouse_autofill_report.json` with every field, how it was filled, and the source of that answer
- `submit/greenhouse_autofill_pre_submit.png`, a high-resolution full-page screenshot captured after the final review page is filled and before submit
- `submit/greenhouse_autofill_pages/page_XX.png`, one high-resolution screenshot per application page before the automation moves forward
- `submit/gem_autofill_report.md` / `submit/gem_autofill_report.json`, `submit/gem_autofill_pre_submit.png`, and `submit/gem_autofill_pages/page_XX.png` for Gem-hosted forms
- `submit/lever_autofill_report.md` / `submit/lever_autofill_report.json`, `submit/lever_autofill_pre_submit.png`, and `submit/lever_autofill_pages/page_XX.png` for Lever-hosted forms
- `submit/ashby_autofill_report.md` / `submit/ashby_autofill_report.json`, `submit/ashby_autofill_pre_submit.png`, and `submit/ashby_autofill_pages/page_XX.png` for Ashby-hosted forms
- `submit/dover_autofill_payload.json`, `submit/dover_application_job.json`, and `submit/dover_submission_response.json` for Dover-hosted `app.dover.com/apply/...` flows, which submit through Dover's public application API instead of a browser fill loop

For the browser-based boards, the JSON/markdown autofill reports now split confirmed `fields` from `planned_but_unconfirmed_fields`. If any entries remain in `planned_but_unconfirmed_fields`, the submitter writes those exact unresolved items into `submit/pending_user_input.json` and fails closed instead of clicking submit.

The screenshot runtime expands nested application scroll containers into the page before capture and suppresses repeated top sticky headers, so app-shell job boards do not produce truncated or duplicated review images.
- `submit/greenhouse_unknown_questions.json` if the live form exposes required questions the autofill payload cannot answer confidently
- `submit/greenhouse_submit_debug.html` and `submit/greenhouse_submit_debug.png` if a submit attempt fails to reach a confirmed completion state
- `submit/gem_unknown_questions.json`, `submit/gem_submit_debug.html`, and `submit/gem_submit_debug.png` for the same failure cases on Gem
- `submit/ashby_unknown_questions.json`, `submit/ashby_submit_debug.html`, and `submit/ashby_submit_debug.png` for the same failure cases on Ashby

For Greenhouse email verification challenges, the runtime uses `gws` (googleworkspace/cli) to read the code from Gmail instead of scraping it out of the browser flow. The lookup is anchored to the current submit attempt and waits briefly for the fresh verification message, which prevents older Greenhouse codes from being reused during retries. Make sure `gws auth login` has already been completed for the mailbox you want to use.

The submit runtime handles both modern Greenhouse flows and classic embed forms. It treats multi-page navigation, same-page email verification prompts, same-page confirmation screens, modern React/ARIA combobox controls, autocomplete fields, and plain free-form text fields as first-class cases instead of assuming a single URL pattern. Direct `job-boards.greenhouse.io/...` pages stay on their native URL and can click a visible page-level `Apply` CTA before waiting for the inline form to mount. When Greenhouse surfaces a security-code field only after the first submit click, the runtime now treats that as the expected next step, fills it from Gmail via `gws`, and performs one final confirmation re-check before declaring the submit attempt failed.

For Greenhouse select2-backed native selects, the runtime now updates the exact underlying option instead of fuzzy-matching placeholder entries such as `--`. This avoids required yes/no custom questions appearing visually filled while still posting blank values at submit time.

Classic Greenhouse embeds can also expose required `question_option_id` selects for custom gates such as work authorization, commute proximity, and in-office availability, plus raw bracketed demographic field names from older EEOC markup. The submitter now infers city-specific options like `Yes, San Francisco` directly from `application_profile.md`, handles state-only location selectors even when the prompt sounds city-based, avoids fuzzy one-word label matches such as `Country` accidentally binding to a different question, and leaves classic bracketed demographic controls to the visible demographic-question runtime instead of trying to type into them as free-form text.

Greenhouse explicit state-list residency gates such as `Do you live in one of the following states?` are answered from the candidate's actual current state in `application_profile.md`, rather than from the generic `Live In Job Location` boolean.

Greenhouse salary/compensation comfort gates such as `Are you comfortable interviewing for the salary outlined in the job description?` are answered from the explicit salary-comfort default in `application_profile.md`.

Across the supported submitters (Greenhouse, Ashby, Lever, Gem, and Dover), explicit salary/compensation-comfort yes/no gates are answered from `application_profile.md`. Explicit free-text salary/compensation expectation prompts now use the shared `Compensation Expectations` answer, and explicit undergraduate/Bachelor's GPA prompts now use the shared `Undergraduate GPA` value (`3.8/4.0`) instead of education-history text or LLM drift. If any of those boards exposes a cover-letter textarea or upload field, the submitter now confirms the text or file is actually present before allowing submit; otherwise the run fails instead of silently skipping the slot.

Across those same supported submitters, explicit transgender-status yes/no prompts now use the `Transgender Status` default in `application_profile.md`. With the current profile, that means those questions are answered `No` rather than falling back to a generic decline response.

When a new job board or company-hosted wrapper is added, it should inherit the same submit criteria immediately: the shared positive-fit screening policy for discrete fit prompts, truthful/profile-driven handling for work authorization, compensation, and self-ID prompts, confirmed resume and cover-letter attachment or fill, fail-closed pending-user-input artifacts for unsupported narrative questions, no submit while planned fields remain unconfirmed, and the same website/email/Notion confirmation artifacts used on the existing boards.

The supported submitters also now use the stricter minimum-experience matcher across boards, so long narrative prompts that merely mention `experience` are no longer reduced to boolean defaults. Unsupported specialized narrative questions fail closed into `submit/pending_user_input.json` until you provide the exact answer to reuse later, and browser-based submitters use that same artifact when a planned answer could not be confirmed on the live form.

Across the supported submitters, discrete positive-fit gates such as `come into the office`, `3 days per week`, `hybrid`, commute-distance prompts, relocation, travel, and direct experience/background confirmations are routed through the shared positive-fit policy instead of falling through as missing required questions. Unsupported degree/license/certification claims remain fail-closed rather than being forced to `Yes`.

Some Greenhouse classic forms also require a visible education row even when the posting itself never mentions education. The submitter now fills one live education entry from `master_resume.md` using the candidate's real school, degree, discipline, and end year, and it targets the rendered form controls instead of hidden template rows or disabled select2 scaffolding.

Visible Greenhouse demographic-question groups such as Gender Identity, Race/Ethnicity, Pronouns, Sexual Orientation, Veteran Status, Disability Status, racial/ethnic background, transgender identity, or veteran/active-member prompts are answered from `application_profile.md`. If one of those groups is required and no explicit configured value exists, the runtime chooses the board's explicit decline / no-answer option instead of stalling on an otherwise complete application. If the profile already has `Transgender Status`, the runtime uses that exact value instead of declining. The runtime now targets the real nested `.demographic_question` groups before broader container fallbacks so classic multi-question EEOC sections do not collapse into one missed block.

When a Greenhouse cover-letter question exposes both an upload control and `or enter manually`, the submitter now prefers the existing cover-letter file. On classic `attach-or-paste` uploaders, it uses the attach path even when the underlying file input only exists after the attach button is clicked. If the board only uses the manual-entry textarea path, the runtime reveals it and confirms the text is present before allowing submit. If a cover-letter slot exists on the application, the run fails rather than silently skipping it.

Runtime-only optional Greenhouse self-ID widgets are now treated as non-blocking. The submitter autofills them only when they were parsed into the explicit payload up front; fallback runtime discovery should not stall or block a submit on optional voluntary self-identification controls.

Across the supported submitters, a yes/no prompt that mentions both work authorization and sponsorship or visas is treated as a sponsorship question first. This prevents future-sponsorship questions from being answered `Yes` just because the candidate is already authorized to work unconditionally.

Gem-hosted forms can present hCaptcha on final submit. In headed mode, the submitter now waits up to five minutes for a manual solve in the browser window and then retries submit automatically instead of failing immediately.

Lever-hosted forms use the static `/apply` page rather than the public posting page for runtime inspection and autofill. The submitter now skips non-fillable Apply-with-LinkedIn widget rows, uses exact word-boundary label matching so short fragments like `city` do not bind to unrelated questions, fills radio/checkbox choices through their underlying inputs when the embedded hCaptcha widget intercepts label clicks, classifies validation errors before treating hCaptcha as the blocker, and pastes long free-text answers directly instead of spending minutes human-typing a cover letter into the `comments` box.

Ashby-hosted forms can present reCAPTCHA on final submit. In headed mode, the submitter waits for a manual solve in the browser window only when an active challenge is visible, retries submit, and records success from website confirmation states including banners such as `Your application was successfully submitted`. Direct Ashby `jobs.ashbyhq.com/.../application` URLs are canonicalized back to the posting URL before scrape so the deterministic pipeline sees the full posting instead of the thinner application shell, and thin same-site application shells now trigger a same-site search for the fuller JD page before extraction fails. Company-hosted `ashby_jid` wrapper URLs also reject host-derived Ashby shell pages whose `window.__appData` has no real `posting` payload, then fall back to the iframe/embed-discovered hosted-jobs slug instead of scraping the wrong direct path.

Ashby-hosted forms can also mix visible Yes/No button groups, radio/value-select groups such as metro-area questions and `Office location of choice` prompts, nested phone-number text-message consent radios, standard EEOC value-select prompts, insurance-background yes/no gates, and long free-text areas backed by hidden inputs. The submitter handles those controls directly, including mapping office-location selects from the configured application location, instead of trying to type into the hidden elements or misclassifying validation errors as CAPTCHA waits.

Dover-hosted `app.dover.com/apply/...` jobs now use Dover's public `application-portal-job` API for JD extraction so the deterministic pipeline avoids the Cloudflare-gated HTML shell. The Dover submitter mirrors the live frontend payload: LinkedIn, resume, and phone stay as top-level multipart fields, only `CUSTOM` questions are serialized into `application_questions`, and `rs` / `referrerSource` from the job URL is forwarded as `referrer_source`.

Greenhouse, Gem, Lever, and Ashby submitters now poll Gmail confirmation in parallel after the final submit click. Dover uses the same shared website/email confirmation and Notion-sync path immediately after its API submit succeeds. This includes company-hosted Greenhouse wrapper URLs that expose `gh_jid` instead of a `greenhouse.io` hostname and company-hosted Ashby wrapper URLs that expose `ashby_jid` instead of a `jobs.ashbyhq.com` hostname. If the website shell stalls or never renders a stable confirmation page but the matching confirmation email arrives, the run treats the application as confirmed and proceeds through the shared Notion-sync path instead of failing on the browser state alone. The `gws` lookup is now time-bounded, HTML-normalized, company-scoped, and anchored to the submit timestamp so a slow or overly broad Gmail query cannot freeze the browser confirmation loop or let a generic provider email from a different application satisfy the current run.

The shared Gmail confirmation matcher now carries the submit-start timestamp through the Notion-sync path and requires a plausible company or board/provider match before it accepts an email. This prevents unrelated older application confirmations from satisfying a new submit run just because they share generic role words like `Product Manager`.

If `submit/application_submission_result.json` already records a website-confirmed application, rerunning `job-assets submit --submit ...` or the same `job-assets pipeline --submit ...` command now skips the duplicate live resubmit instead of applying twice and resumes only the email/Notion reconciliation work. If `submit/notion_sync_status.json` was just updated with a `pending_email_confirmation` result, reruns also skip another full Gmail wait until the pending-email cooldown expires.

When `submit/application_answers.json` already exists and the live question list has not changed, submit retries reuse the cached generated answers instead of invoking the provider again. Submit-time answer generation stays bounded by `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` (default `600`, set `0` to disable), while full resume and cover-letter generation uses `JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS` (default `1200`, falling back to the general timeout override when only `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` is set). Asset generation now runs in two provider steps: company research first, then resume and cover-letter drafting from the saved `output/<company>/research_cache.json`. By default Claude gets 600 seconds to begin each research, drafting, or fix pass; if it exits or times out before writing the expected files, the CLI retries that pass once with Codex. Submit-time answer generation also retries once with Codex when Claude fails before returning valid JSON, writing the retry transcript to `submit/application_answers_fallback_raw.txt`. Override the asset-generation behavior with `JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS` and `JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER`, including `0` / empty to disable the retry entirely. Repo entrypoints default `UV_CACHE_DIR` to `./.uv-cache` when it is unset, which keeps `uv run ...` invocations off the global cache path in both local and sandboxed runs. Provider subtasks that are asked to produce `resume_content.json` or `cover_letter_text.txt` are also blocked from recursively relaunching `job-assets`, `apply.sh`, or the pipeline scripts; they must write those files directly from the prepared inputs. Asset-generation raw provider output is written to `content/llm_research_raw.txt`, `content/llm_drafting_raw.txt`, and `content/llm_fix_attempt_<n>_raw.txt`, and fallback retries write sibling `*_fallback_raw.txt` artifacts. If the provider exits non-zero after already writing updated content files, the pipeline finalizes and continues instead of throwing those artifacts away.

Board submitters now prefer a persistent local Chrome-backed Playwright profile at `~/.job-assets/playwright-submit-profile` before falling back to bundled Chromium, which reduces avoidable CAPTCHA challenges caused by brand-new ephemeral browser sessions. Override the profile path with `JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR` or the interaction pacing with `JOB_ASSETS_SUBMIT_SLOW_MO_MS`. On macOS, headed local Chrome launches keep the native GUI environment instead of rewriting `HOME` or `XDG_*`; the persistent Playwright user-data dir already isolates the session, and synthetic home directories can crash AppKit/HIServices startup there. Headless local Chrome launches on macOS now use an isolated submit-home again so Crashpad and related Chrome state do not point at sandbox-protected user directories. If a persistent-profile macOS launch still aborts early, the launcher now retries once without persistence so the submit flow can keep going. The local-browser launcher also deduplicates equivalent browser-family attempts, so a failed Google Chrome startup is not immediately retried through both the Playwright channel and the same app executable. Headed macOS submit flows also skip the bundled Playwright `Google Chrome for Testing` fallback entirely, because that cache-installed app can abort during AppKit/HIServices startup; use an installed local Chrome / Chromium / Edge browser or `JOB_ASSETS_BROWSER_PROVIDER=steel` instead.

Post-submit tracker sync is now board-agnostic. Any submitter that writes `submit/application_submission_result.json` can reuse `job-assets notion-sync ...`; the Greenhouse flow already does this automatically after a confirmed `--submit`. The shared sync also writes `submit/application_confirmation_website.json`, `submit/application_confirmation_email.json`, and `submit/notion_sync_status.json` so delayed email arrivals or missing Notion auth are explicit instead of silent.

Standalone `job-assets notion-sync ...` reruns now anchor their Gmail search to the recorded website-confirmation timestamp and clear stale `submit/application_confirmation_email.json` artifacts when no fresh match exists, so older unrelated Greenhouse emails cannot be reused accidentally during a later resync.

If the confirmation email arrives later, rerun the sync manually:

```bash
job-assets notion-sync output/company/role-slug --allow-pending-email
```

The Notion sync discovers the live database schema from the target data source, sets the row to `Applied`, updates `Application Date`, preserves existing rows when the job URL or board title already exists, and appends the JD plus application metadata when it has to create a new page.

If Chrome is not available locally, install a Playwright browser once:

```bash
uv run playwright install chromium
```

## Built-in help

```bash
job-assets --help
job-assets apply --help
job-assets pipeline --help
job-assets batch --help
job-assets parallel --help
job-assets submit --help
job-assets profile --help
job-assets notion-sync --help
job-assets man
job-assets-codex man
job-assets-claude man
```

## Environment knobs

Default provider:

```bash
export ASSET_LLM_PROVIDER=codex
```

`job-assets`, the shell entrypoints, and the shared Notion sync scripts automatically load local values from `.env` and `.env.local` in the repo root. Keep secrets in `.env.local`; it is gitignored and meant for machine-local credentials that should persist across CLI runs and future agent sessions.

Without any overrides, the repo now invokes:

```text
claude  -> claude-sonnet-4-6 with --effort max
codex   -> gpt-5.4 with model_reasoning_effort="xhigh"
```

Optional model overrides:

```bash
export CLAUDE_MODEL=claude-sonnet-4-6
export CLAUDE_EFFORT=max
export CLAUDE_PERMISSION_MODE=auto
export CODEX_MODEL=gpt-5.4
export CODEX_REASONING_EFFORT=xhigh
export CODEX_APPROVAL_POLICY=never
export CODEX_SANDBOX_MODE=danger-full-access
export CODEX_PROFILE=your-profile
export NOTION_API_TOKEN=secret_...
export NOTION_JOB_APPLICATIONS_DATA_SOURCE_ID=2e238885-a751-802d-8274-000bd78e05b4
export NOTION_JOB_APPLICATIONS_DATABASE_ID=2e238885-a751-80cd-bd2c-da1a28dc3edb
```

Codex runs through `codex exec` with `--skip-git-repo-check`, explicit `--ask-for-approval` and `--sandbox` settings, `model_reasoning_effort="xhigh"`, and `--search` for research tasks. The end-to-end CLI mirrors the Codex TUI as closely as repo automation allows by defaulting those execution settings to the same values in `~/.codex/config.toml` when available, and otherwise to `never` plus `danger-full-access`. Claude runs through `claude --permission-mode auto --model claude-sonnet-4-6 --effort max --print`.
Non-interactive Claude runs also pin `--setting-sources project,local`, disable session persistence and slash commands, pass a strict empty MCP config so local user plugins or saved interactive session state do not leak into repo automation, and keep `--print` immediately before the prompt because some current Claude CLI builds drop the prompt when invoked as `claude -p ... prompt`. Provider argv and mode-specific search/tool limits are centralized in `scripts/llm_provider.py` so asset generation and submit-time answer generation stay aligned across boards. Asset generation is split into a research pass and a drafting pass, with drafting reading `output/<company>/research_cache.json` from the research pass. By default Claude gets 600 seconds to begin each research, drafting, or fix pass, then retries once with Codex if it exits or times out before writing the expected files. Submit-time generated application answers use the same timeout budget, and Greenhouse now follows the same timeout-plus-fallback behavior as the shared submitters. Override the asset-generation behavior with `JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS` and `JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER`, including `0` / empty to disable the retry entirely. Provider subtasks are blocked from recursively relaunching repo entrypoints such as `job-assets`, `apply.sh`, and the pipeline scripts; they must write the requested content files directly. Any fallback retry writes its transcript to the matching `*_fallback_raw.txt` artifact.
If a Codex CLI upgrade starts rejecting the generated non-interactive argv, compare `codex --help` with `codex exec --help` before changing the builder. Approval, sandbox, search, and working-directory flags have moved between the top-level command and `exec` across versions.
Use `job-assets doctor` to print the effective provider defaults after local env overrides are loaded.

## Authentication

These commands assume the relevant CLI is already installed and authenticated once:

- `claude` for Claude Code
- `codex` for Codex CLI

After that, the repo CLI invokes the provider directly.
