# Operational Rules

These rules apply to **all providers** (Claude, Gemini, Codex/GPT) when working on this project.

---

## Applying to Jobs — Standing Orders

When the user asks to apply to one or more jobs (via CLI, TUI, or directly), follow these rules without prompting:

1. **Default to `--draft`.** "Apply" means generate materials and fill the application form, but stop before submitting. Present the draft to the user and only submit with `--submit` after explicit approval. If the user explicitly says "submit" or "auto-submit", skip the draft review and use `--submit` directly.
2. **Resolve JD from application URLs.** If given a direct application link with no JD, derive the JD URL from the URL structure or career site.
3. **Unsupported boards → build support.** If the job board isn't supported, implement it following the existing autofill architecture.
4. **Blockers → log and move on.** For captchas, account locks, or questions with very low confidence: log to `submit/manual_review.json` with the issue, context, and resolution suggestions. Then continue to the next job.
5. **Low-confidence answers → best guess + log.** For questions where you have some confidence but aren't sure, fill your best answer and log it to `submit/manual_review.json` with your reasoning and alternatives.

---

## Post-Fix Workflow — Mandatory After Every Fix

Every fix must go through ALL of these steps, no exceptions:

1. **Generalize across all boards and runtimes** — the fix must apply to every board (Greenhouse, Ashby, Lever, Gem, Dover, Workday, Phenom, iCIMS, Eightfold, BambooHR, SmartRecruiters, Workable, Comeet, Rippling, Uber, Motion Recruitment, Reducto, LinkedIn Easy Apply) and every runtime method: CLI, TUI, worker, web app, and direct LLM runs (`claude`, `gemini`, GPT). Never fix just one board or one execution path.
2. **Update all provider instructions** — Update AGENTS.md with new patterns, then regenerate all provider copies with `uv run python scripts/sync_agent_files.py`. The generated set is `CLAUDE.md`, `GEMINI.md`, `CODEX.md`, `GPT.md`, and `.github/copilot-instructions.md`. Never edit generated files directly.
3. **Track, commit, push, merge** — `git add`, commit with a descriptive message, `git push`. Do this after each individual fix, not batched at the end.

## Backlog Sweeps

For stopped/draft sweeps larger than 25 rows, start a fresh run with `uv run python scripts/init_backlog_sweep.py --new-run` before Phase 1, start Phase 2 and Phase 3 with their own phase-start snapshot commands, keep append-friendly results ledgers current, use `uv run python scripts/check_backlog_sweep.py --active` as the fast coverage gate, and do not claim completion unless `uv run python scripts/verify_active_sweep.py --active` passes. See [`backlog-sweep.md`](backlog-sweep.md).

---

## Draft Review (LLM Runtime)

When the pipeline generates a draft (stops before submitting), present the review to the user:

1. Read and display the full `draft_summary.md` inline in conversation
2. Read and show the pre-submit screenshot image (`submit/{board}_autofill_pre_submit.png`)
3. Prompt: "Draft ready. Review the answers above. You can: approve, reject, edit specific answers, or describe issues for me to fix."
4. If user describes issues -> apply generalized code fixes across all boards -> user says "regenerate" -> re-run pipeline
5. If user edits `draft_summary.md` directly -> diff changes, classify as missing_handler or wrong_answer, apply code fixes
6. If user approves -> run `submit_application.py --submit` on the output directory to resume submission
