# Agent Preferences

Behavioral defaults and working conventions for this repo.

## Workflow defaults

- Prefer `--draft` unless the user explicitly requests live submission.
- Treat a role as draft-ready only when the generated materials, autofill report, and screenshot proof all exist.
- Screenshots are the verification source of truth.
- Re-run the canonical role directory before reporting a durable fix.
- Do not re-run a submission pipeline merely to repair a secondary sync or bookkeeping artifact if a targeted follow-up command exists.

## Candidate data rules

- Load candidate-specific details from runtime inputs or settings, not repo constants.
- Never ask the user to recreate source materials that the runtime already owns.
- If runtime files are missing, use neutral placeholders and surface the gap clearly instead of inventing personal data.

## Form-filling defaults

- Positive-fit screening prompts default to affirmative answers when the prompt is a discrete fit check.
- Negative disclosure prompts default to `No` unless runtime inputs say otherwise.
- Compensation answers should stay non-numeric unless a numeric answer is explicitly provided by the user or runtime profile.
- If a question requires domain-specific detail the automation cannot safely infer, stop and surface the missing input instead of guessing.

## Working style

- Generalize fixes across supported boards and surfaces when the same issue pattern appears.
- Keep changes small, direct, and reviewable.
- Update `AGENTS.md` and regenerate provider copies after durable instruction changes.
