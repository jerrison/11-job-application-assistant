# Agent Preferences

Behavioral defaults, form-filling rules, and working style preferences. These apply to **all providers** (Claude, Gemini, Codex/GPT) and **all runtime modes** (CLI, TUI, worker, web app, direct LLM runs).

Read by agents when they need context on how to behave. Referenced from AGENTS.md.

## Workflow Preferences

- Always use `--draft`; never auto-submit unless user explicitly says "submit". Submitting is irreversible and visible to employers.
- "Draft" means the FULL pipeline end-to-end: generate resume + cover letter, fill the application form (autofill), generate answers to all questions, take screenshot of the filled form, and stop before clicking submit. A job is only "draft ready" if resume, cover letter, autofill report, and screenshot all exist.
- Auto-retry transient failures (rate limits, timeouts, auto-fix busy) automatically via `_auto_retry_if_transient()` up to `MAX_AUTO_RETRIES`. After exhaustion, flag as `needs_manual` with a user-friendly suggestion. Never leave transient failures in `failed` status expecting manual restart.
- Never re-run the submission pipeline to fix Notion sync — it sends a duplicate application. Run `uv run python scripts/notion_sync.py <out_dir>` directly instead.
- Auto-mark applied on LinkedIn after confirmed submission so the user doesn't accidentally re-apply.
- LinkedIn Easy Apply: never check "Follow company", never mark a job as "priority". These are noise/spam signals.
- LinkedIn Easy Apply resume handling: when LinkedIn exposes a visible upload/change resume path, always re-upload the current role's employer-named resume and require the live UI to show it as the selected attachment. If Review is reachable without any visible upload/change path, continue but label the artifacts as unverified fresh-upload state rather than treating it as confirmed.
- Screenshots are source of truth for verifying autofill, not the autofill report JSON. The report may say "filled" even when the runtime silently skipped a field (e.g., input element not found in React apps).
- After any temp-copy or debug rerun used to diagnose a draft/autofill issue, always rerun the canonical role directory before reporting completion. Treat the canonical `output/...` role as the durable source of truth.
- When the user asks to redraft a role, complete the canonical redraft itself before reporting back. Do not stop at a temp verification copy or synthetic fixture.
- For Linear or similar tickets, keep verification proof self-contained in the ticket body. Do not rely on downloadable log attachments for core evidence when a concise inline summary will fit.
- When attaching screenshot proof to a ticket, capture the affected UI region itself (for example the Screenshot tab content), not surrounding worker controls or unrelated page chrome.
- When a user links a tracked issue and asks for a fix, treat the workflow as mandatory: generalize the code change across boards and surfaces, rerun the canonical affected role in `--draft`, inspect the fresh pre-submit screenshot, verify the web UI review surface and the `approve_submit`-ready state for the affected job, then post self-contained proof in the ticket before reporting completion.

## Prose Style

- For generated cover letter body text and generated application answers, avoid the Unicode em dash character (`—`) when possible. Prefer commas, periods, parentheses, or hyphens instead.
- Preserve em dashes only in direct quotes or fixed text copied verbatim from user or source material.
- This preference does not apply to filenames, resume content, code, comments, copied user text, or other verbatim source excerpts kept unchanged.

## Form Filling Defaults

- **Workday auth flow:** Sign in first with credentials from `.env.local` → password reset via `gws` CLI if wrong → create account as fallback → email verification via `gws` CLI if needed. Always choose "Apply Manually" first, then "Autofill with Resume" as fallback.
- **Combobox/dropdown fields:** Click to expand first, read all available options, then pick the best match. Never type-to-filter for client-side option lists (fragile due to React state and timing). Only use type-to-search for server-side search fields (like school name) where options load on input.
- **Culture/careers opt-in questions:** Always say Yes. Shows genuine interest in the company.
- **Positive-fit screening questions:** Across all supported boards and surfaces, answer discrete positive-fit screening prompts affirmatively by default. This includes hybrid/in-office/commute questions, relocation and travel willingness, product-usage prompts, and general experience/background confirmations. Do not flatten open-ended prompts into `Yes`. Degree/license/certification claims only answer `Yes` when supported by `application_profile.md` or explicit credential signal in `master_resume.md`.
- **Negative disclosure screening questions:** Across all supported boards and surfaces, answer non-compete / restrictive-covenant prompts, employee-or-vendor relationship disclosures, outside-business / outside-commitment disclosures, investment-conflict disclosures, and IP-retention disclosures with `No` by default unless repo sources or explicit user input say otherwise.
- **Bay Area location fields:** Use "San Francisco, CA" for any job in the SF Bay Area (San Francisco, South Bay, Peninsula, East Bay, etc.).
- **Compensation questions:** Never give a numeric salary amount. For free text: "I'm open and flexible on compensation. I'd prefer to learn more about the role's scope and total rewards package before discussing specific numbers." For numeric-only fields that require a number: flag the issue rather than inventing a number.
- **"Influence/decision to apply" priority:** For questions like "What was the largest influence on your decision to apply?" or "What attracted you to the company?", prioritize: (1) Culture/Values, (2) Company vision/mission, (3) Company leadership. Fall back to "The product" or "The role" only if none of the top 3 are available.
- **"How did you hear about us" priority:** (1) `application_profile.how_did_you_hear` value (currently "Corporate website"), (2) company-specific website variants ("{Company} Website", "Company Website", "Corporate website", "Career Site", "Website"), (3) blog variants ("{Company} Blog", "Company Blog", "Blog"), (4) LinkedIn, Job Board, Social Media, Other.
- **Employment status:** Full-time employment (not self-employed).
- **Languages spoken:** English, Spanish, Mandarin, Cantonese. When filling language fields: "Chinese" covers both Mandarin and Cantonese; select all matching options from the available list. If free-text, enter "English, Spanish, Mandarin, Cantonese".
- **Preferred work locations** (ranked): (1) San Francisco, CA, (2) New York, NY, (3) Los Angeles, CA, (4) Miami, FL, (5) London. For multi-select location fields, select all matching cities from this list. For single-select, use "San Francisco, CA". Never select cities not on this list (e.g., Boston).

## Code Change Rules

- Every fix must be generalized across ALL supported job boards (18+) and ALL runtime paths (CLI, TUI, worker, web app, direct LLM runs). A fix to one board's location handling must be checked and applied across all boards.
- After 3+ similar per-board implementations, proactively suggest a generic/unified approach. ("We now have enough examples to see the common pattern -- should we build a generic version instead of continuing to add specific ones?")
- Edit AGENTS.md and run `uv run python scripts/sync_agent_files.py`. Never edit generated files directly.
- Commit, merge to main, and push after every individual fix or feature completion. Don't batch changes or wait for explicit "commit" instruction. Treat landing the change as part of completing the task.
- Commit frequently in small, logical chunks. Each commit should be reviewable on its own. PR descriptions must link to the higher-level goal and explain *why*, not just *what*.
- For tracked bug fixes, write commit messages and bodies that explicitly cover three things: what the issue was, what was fixed, and why the fix addresses the root cause.

## Candidate Context

- Speaks English, Spanish, Mandarin, Cantonese
- Employed full-time (not self-employed)
- Default LLM provider for pipeline: **OpenAI API** (`ASSET_LLM_PROVIDER=openai`), model `gpt-5.4`. Automated drafting / answer-generation fallback is restricted to **OpenAI → Gemini** even if older env values mention Claude or Codex. `ASSET_LLM_PROVIDER` still controls the primary provider for explicit runs, and the CLI still recognizes `claude`, `codex`, `openai`, `gemini`, and `gemini-flash`. Gemini defaults to `gemini-3-flash-preview`. Auto-fix and interview prep use the configured provider too.
- Google account: jerrisonli@gmail.com

## Working Style

- Spawn subagents aggressively to minimize main context window usage. Use Explore subagents for codebase research, general-purpose subagents for multi-file edits. Run independent subagents in parallel. Only bring back concise summaries, not raw data.
- Small logical commits; PRs must explain why and link to specs.
- Use Playwright MCP browser tools when WebFetch returns 403 or other access errors. On any 403/blocked response, immediately fall back to Playwright: `browser_navigate` to the URL, then `browser_snapshot` to read the content. Don't waste time retrying WebFetch or searching for mirrors.
- Use `gws` CLI (`/opt/homebrew/bin/gws`) for all Google Docs and Gmail interactions. Never use Playwright or browser automation for Google services. Commands: `gws docs ...` for Docs, `gws gmail ...` for Gmail. For editing: `gws docs documents batchUpdate`. For reading: `gws docs documents get`. If any application flow is blocked by an email verification or security code, always check the inbox with `gws gmail ...` before treating the run as blocked or asking the user for the code.

## Job Discovery Integration

Job discovery and scoring features are part of this project (not a separate system). Flow: discover candidate jobs, score/rank them, user selects, auto-draft (full pipeline through autofill, stops at draft for review). Never auto-submit from discovery.

## Known Bugs

- **"Kill All" button orphans workers:** The web UI "Kill All" (`POST /api/kill`) only kills the web server process via `os._exit(0)`. It does NOT call `stop_workers()` first, so all workers, claude subagents, apply.sh, and run_pipeline.py continue as orphans consuming API tokens. Fix: call `stop_workers()` before `os._exit(0)` in the `/api/kill` endpoint.

## Agent Tone

Be direct, professional, and efficient. Confirm inputs, deliver the files, show the changelog (for resume) and the letter text (for cover letter), ask if adjustments are needed. Don't narrate your thinking process unless the user asks.

## Candidate Info (Do Not Ask For)

The candidate's name, contact info, work history, education, and skills are all available in `master_resume.md`, broader narrative context is available in `candidate_context.md` plus the synced Google Docs, and application-form defaults are available in `application_profile.md`. Use them directly. Never ask the user to provide their resume or background -- you already have it.

## Preference Evolution

When any provider session discovers a correction or new preference:
1. Apply it immediately in the current session
2. Add it to `agent_preferences.md` in the appropriate section
3. Commit in the same PR as any code change
This ensures all providers learn from every correction, not just Claude.
