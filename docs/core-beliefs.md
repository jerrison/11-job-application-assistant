# Core Beliefs

Golden principles for this codebase. Opinionated, mechanical rules that keep agent runs legible and consistent.

---

## Architecture

1. **Composition, not inheritance.** Board scripts import shared utilities from `autofill_common.py` and opt into orchestration via `autofill_pipeline.py`. No base classes, no abstract methods.
2. **Dependency direction flows one way.** `autofill_common` < `application_submit_common` < `autofill_pipeline` < board scripts. Nothing flows backward; no circular imports.
3. **Board-specific logic stays board-specific.** Selectors, form parsing, and field inference live in `autofill_{board}.py`. If two boards need the same logic, extract it to `autofill_common.py` — never have one board script import from another.
4. **One canonical source for constants.** Provider names live in `VALID_PROVIDERS` in `llm_provider.py`. Board detection lives in `job_board_urls.py`. No hardcoded duplicates elsewhere.
5. **Output structure is a contract.** `output/{company}/{role-slug}/content/` for intermediate artifacts, `documents/` for final deliverables, `submit/` for submission state. Scripts must not invent new top-level directories.

## Code Quality

6. **Validate at boundaries, trust internally.** Parse and validate inputs (URLs, JD content, LLM responses) at the entry point. Once validated, downstream functions can assume well-formed data.
7. **Deterministic overrides beat LLM answers.** If a question can be answered mechanically (product usage, salary comfort, pronouns, demographics), use a deterministic handler. LLM is the fallback, not the default.
8. **No bare `python`.** Always `uv run python`. No exceptions, including in scripts, CI, and documentation examples.
9. **Tests accompany changes.** Every behavioral change ships with a test or an update to an existing test. Run `uv run python -m pytest tests/ -v` before declaring done.
10. **Cached answers still run through overrides.** New deterministic rules must apply to cached/reused answers, not just freshly generated ones.

## Agent Legibility

11. **The repo is the system of record.** If a pattern, convention, or gotcha isn't in the repo (AGENTS.md, agent_preferences.md, docs/, or code comments), it doesn't exist. No tribal knowledge in Slack, Notion, or conversations.
12. **Progressive disclosure.** AGENTS.md is the table of contents. Generated provider copies carry the same prompt to Claude, GPT/Codex, Gemini, and Copilot. `docs/` holds details. Code is the ground truth. An agent reads them in that order and stops when it has enough context.
13. **File names communicate purpose.** `autofill_greenhouse.py` fills Greenhouse forms. `job_board_urls.py` handles URL canonicalization. A file should do what its name says and nothing else.
14. **Small focused files over monoliths.** If a file grows past ~500 lines, look for an extraction. Each file should have a single reason to change.
15. **Patterns, not magic.** A new contributor (human or agent) should be able to add a new board by reading one existing board script and `autofill_common.py`. No hidden setup, no implicit registration.

## Operational

16. **Every fix generalizes across all boards.** A bug in Greenhouse's checkbox handling likely exists in Ashby's too. Fix the pattern in `autofill_common.py`, not just the one board that triggered the bug.
17. **Draft before submit.** `--draft` is the default. Never auto-submit without explicit approval. Production applications are irreversible.
18. **Idempotent reruns.** Running the pipeline twice on the same job must produce the same result. Caches, deduplication, and deterministic overrides make this possible.
19. **Graceful degradation, never hard stops.** Captcha encountered: skip and log. Unsupported board: log and continue. Provider fails: try the next one in the chain. Batch runs must not die on a single job's failure.
20. **Log what you skip.** Every skip (captcha, unsupported board, low-confidence answer) writes structured JSON to `submit/` with context and resolution suggestions. Silent failures are bugs.

## Documentation

21. **AGENTS.md is the single source of truth.** CLAUDE.md, GEMINI.md, CODEX.md, GPT.md, and copilot-instructions.md are identical generated copies. CI enforces parity.
22. **AGENTS.md is a ~100-line navigation map.** Detail lives in docs/.
23. **Plans are first-class versioned artifacts.** Active execution plans live in `docs/exec-plans/active/`, completed ones move to `docs/exec-plans/completed/`, and the template lives at `docs/PLAN_TEMPLATE.md`.
24. **Update docs in the same commit as the code change.** If a fix changes behavior documented in `docs/autofill-patterns.md` or `docs/board-architecture.md`, the doc update ships in the same commit.
