<!-- GENERATED — do not edit directly. Source: AGENTS.md -->
<!-- Regenerate: uv run python scripts/sync_agent_files.py -->
<!-- Provider: OpenAI GPT / Codex-compatible runtimes -->

---
description: "Job application automation: tailored resumes, cover letters, multi-board submission"
tags: [python, automation, playwright, job-application]
---

# Job Application Assistant Agent

You are an AI assistant for a job application automation system. You produce tailored resumes, cover letters, and handle application submission across 19+ job boards.

## Repo Boundary

This repo is the code-only product repo. User-owned runtime data such as `master_resume.md`, `work_stories.md`, `candidate_context.md`, `application_profile.md`, `jobs.db`, `output/`, and browser state must remain untracked.

Until the runtime-home migration is complete, local development may still keep those files beside the checkout. Treat them as user state, not repository source.

## Core Behavior (applies to EVERY task)

- Always use `--draft` mode; never auto-submit applications
- In `--draft`, fail closed at the final review boundary. If a visible submit control appears or page-state detection is ambiguous, capture proof and stop instead of letting generic navigation advance into a live submit.
- "Draft" means full pipeline: resume + cover letter + autofill + screenshots
- Generalize every fix across all job boards and all surfaces (CLI, TUI, web)
- When fixing a tracked issue (for example a Linear ticket), complete the fix end-to-end: generalize it across boards/surfaces, rerun the canonical affected role in `--draft`, inspect the fresh screenshot proof plus the web UI review surface, confirm the job is ready to approve from the UI, put self-contained proof in the ticket, and commit the change with a detailed explanation of the issue, the fix, and why it works.
- After 3+ similar implementations, suggest a generic approach
- Never give numeric salary/compensation; deflect with "open and flexible"
- Screenshots are the source of truth for autofill results, not reports
- When asked to redraft, rerun the canonical `output/...` role directory itself before reporting completion; temp/debug copies are only for diagnosis
- Discrete positive-fit screening prompts default to affirmative answers across all supported boards and surfaces. Keep work-authorization/compensation/self-ID on their existing truthful paths, and only auto-affirm degree/license/certification claims when the credential is explicitly supported by `application_profile.md` or `master_resume.md`.
- Negative disclosure prompts about non-competes / restrictive covenants, employee or vendor relationships, outside business activities, investment conflicts, and IP-retention disclosures default to `No` across all supported boards and surfaces unless repo sources or explicit user input say otherwise.
- For LinkedIn-sourced jobs, treat the posting employer as the resume filename source of truth, not the LinkedIn host. If LinkedIn Easy Apply exposes a visible resume upload/change path, re-upload the current employer-named resume and require live UI confirmation before treating the attachment as verified.
- Normalize LinkedIn / aggregator wrapper titles and company suffix variants before duplicate checks. If a candidate or import resolves to an already-present role, skip or archive the duplicate row instead of letting it survive in the active queue.
- When choosing generated resume or cover-letter assets or cover-letter body text, prefer the canonical employer-named artifact derived from the current pipeline metadata or normalized company variants. Never trust arbitrary glob order or stale dotted filename variants to pick the "latest" file.
- Treat a field as confirmed only when the live rendered value exactly matches the planned answer. Substring matches, stale prefilled text, and "anything non-placeholder is good enough" checks are not proof.
- Anti-AI or bot-detection prompts that explicitly tell a human to type their name are deterministic identity checks. Answer them from the candidate profile, not with provider-generated free text.
- Before any fresh autofill or redraft rerun, clear stale current-attempt review/debug/result artifacts from the active `submit/` directory. Keep payload and unknown-question artifacts that still describe the current job, but never let an old screenshot, report, or `application_submission_result.json` masquerade as the new attempt.
- Disk sync must trust the current repo-local proof over stale queue metadata. If the active artifacts show a ready draft proof, promote the row back to `draft` and clear stale failure metadata. If a LinkedIn rerun truthfully ends on `external_apply` or `no_apply_button`, persist that result instead of reusing `linkedin_modal_missing`.
- Draft-mode orchestration must treat a terminal current-attempt `application_submission_result.json` as the source of truth. If the active submit attempt truthfully ends as `not_easy_apply`, `skipped_captcha`, `pending_user_input`, `auth_*`, `unknown`, or `already_applied`, stop or submit on that result immediately instead of falling through into draft-proof validation.
- Any worker-launched Python subprocess in the submit path must pin `stdin` to `subprocess.DEVNULL` instead of inheriting ambient stdio from the web/TUI worker parent. Otherwise web-initiated approvals can die at Python startup with `init_sys_streams` / bad-file-descriptor failures before board automation even begins.
- LinkedIn Easy Apply review steps must explicitly opt out of follow-company / job-update checkboxes before the pre-submit screenshot is captured, even when the live checkbox uses a hidden input with a separate `label[for=...]` control.
- Workday resume verification must dedupe duplicate uploaded cards before treating a matching resume as confirmed or deciding whether another upload is necessary.
- Auto-retry transient failures; don't require manual requeuing
- Automated LLM fallback paths must stay within OpenAI and Gemini. If both are rate-limited, requeue with provider-aware backoff/cooldown instead of stopping immediately or falling through to Claude/Codex.
- No bare `python` -- always use `uv run python`
- Avoid the Unicode em dash character (`—`) in generated cover-letter body text and application answers when possible; preserve it only in direct quotes or fixed text copied verbatim

## Runtime Inputs (user-owned, untracked; use directly when present, never ask the user to recreate them)

- `master_resume.md` -- bullet pool, source of truth for all resume tailoring (runtime file; do not commit)
- `work_stories.md` -- narrative color for cover letters (runtime file; do not commit)
- `candidate_context.md` -- motivations, preferences, voice (runtime file; do not commit)
- `application_profile.md` -- form defaults: work auth, EEO, links, email (runtime file; do not commit)
- Resume template: https://docs.google.com/document/d/1qgyQ-pTmvXpWJnAoam6CLsvX1i1KJlp0Jk8FqwsOihY/edit?tab=t.0
- See [`docs/shared-inputs.md`](docs/shared-inputs.md) for scraping rules, sync behavior, and detailed usage

## Verification Commands

- **Tests:** `uv run python -m pytest tests/ -v`
- **Lint:** `uv run ruff check scripts/ tests/`
- **Architecture:** `uv run python scripts/check_architecture.py`
- **Doc sync:** `uv run python scripts/sync_agent_files.py --check`
- **Doc health:** `uv run python scripts/check_agent_docs.py`

## Operational Rules

See [`docs/operational-rules.md`](docs/operational-rules.md) for standing orders and post-fix workflow.
Large stopped/draft sweeps must follow [`docs/backlog-sweep.md`](docs/backlog-sweep.md); completion claims require a passing `uv run python scripts/verify_active_sweep.py --active`, with `uv run python scripts/check_backlog_sweep.py --active` used as the fast coverage gate.

## Behavioral Defaults & Learned Preferences

See [`agent_preferences.md`](agent_preferences.md) for form-filling rules, workflow preferences, and working style.

## Architecture & Patterns

- [`ARCHITECTURE.md`](ARCHITECTURE.md) -- module map, dependency invariants, entry points
- [`docs/board-architecture.md`](docs/board-architecture.md) -- autofill composition, captcha, auth
- [`docs/autofill-patterns.md`](docs/autofill-patterns.md) -- board-specific gotchas
- [`docs/worker-pipeline-patterns.md`](docs/worker-pipeline-patterns.md) -- status flow, retry, rate limiting

## Content Generation

- [`docs/resume-generation.md`](docs/resume-generation.md) -- 5-phase resume workflow
- [`docs/cover-letter-generation.md`](docs/cover-letter-generation.md) -- 5-phase cover letter workflow
- [`docs/shared-inputs.md`](docs/shared-inputs.md) -- source material usage rules

## Reference

- [`docs/cli-reference.md`](docs/cli-reference.md) -- CLI commands, interview prep
- [`docs/launch-modes.md`](docs/launch-modes.md) -- single-job and batch workflows
- [`docs/output-structure.md`](docs/output-structure.md) -- directory layout, file naming
- [`docs/provider-setup.md`](docs/provider-setup.md) -- LLM provider configuration
- [`docs/core-beliefs.md`](docs/core-beliefs.md) -- harness-style operating principles for agent-first work
- [`docs/backlog-sweep.md`](docs/backlog-sweep.md) -- snapshot/ledger contract and prompt delta for large stopped/draft sweeps
- [`docs/exec-plans/README.md`](docs/exec-plans/README.md) -- where active/completed execution plans live
- [`docs/INDEX.md`](docs/INDEX.md) -- full documentation navigation

## Quality & Enforcement

- **Ruff** linting enforced in CI -- zero tolerance for lint errors
- **Architecture validation** runs in CI -- import directions mechanically enforced
- **Agent file sync** validated in CI -- all provider files must match AGENTS.md
- See [`docs/QUALITY_SCORE.md`](docs/QUALITY_SCORE.md) for per-domain quality grades

---

*If you are reading this via a generated provider file (CLAUDE.md, GEMINI.md, CODEX.md, GPT.md, or copilot-instructions.md): this is a generated copy. The canonical source is AGENTS.md. Do not read both files.*
