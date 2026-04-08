# Agent Context And Harness Hardening

This execution plan is a completed record of the 2026-03-30 hardening pass for provider instruction parity, runtime prompt guidance, and harness-style repo scaffolding.

## Purpose / Big Picture

Make the repo's agent context model match the current harness direction more closely: one canonical `AGENTS.md`, generated provider copies for every actively supported provider surface, runtime prompts that treat `AGENTS.md` as a navigation map instead of an encyclopedia, and tracked execution-plan scaffolding that lives in the repo.

## Context and Orientation

- **Primary files:** `scripts/sync_agent_files.py`, `scripts/check_agent_docs.py`, `scripts/llm_common.sh`, `AGENTS.md`, `docs/INDEX.md`, `docs/core-beliefs.md`
- **Constraints:** Keep `AGENTS.md` concise, generalize the fix across Claude/GPT/Codex/Gemini/Copilot surfaces, and avoid rewriting historical plans/specs just to normalize old wording.
- **External guidance used:** OpenAI's harness-engineering article, the AGENTS.md guide, the subagents concept page, and the execution-plans cookbook article.

## Milestones

1. **Provider copy parity:** Add a GPT-facing generated alias, regenerate provider copies, and prove all generated bodies match `AGENTS.md`.
2. **Runtime prompt alignment:** Update the drafting/research prompt builder so provider runs treat `AGENTS.md` as a map and explicitly read the deeper docs they need.
3. **Harness scaffolding:** Add tracked `docs/exec-plans/` structure, strengthen the plan template, and enforce the scaffold mechanically in `check_agent_docs.py`.
4. **Live-doc cleanup:** Normalize the current repo-facing docs to the "edit `AGENTS.md`, regenerate everything" model and remove stale provider subset language from active docs.

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Add GPT generated copy support | Done | 2026-03-30 |
| Add execution-plan directories + README | Done | 2026-03-30 |
| Update plan template and doc checks | Done | 2026-03-30 |
| Align runtime prompt builder with progressive disclosure | Done | 2026-03-30 |
| Normalize active docs/tests to new contract | Done | 2026-03-30 |
| Run verification suite | Done | 2026-03-30 |

## File Structure

### New files:
- `GPT.md` — generated OpenAI GPT-facing instruction alias derived from `AGENTS.md`
- `docs/exec-plans/README.md` — execution-plan usage guide
- `docs/exec-plans/active/.gitkeep` — track active exec-plan directory
- `docs/exec-plans/completed/.gitkeep` — track completed exec-plan directory

### Modified files:
- `scripts/sync_agent_files.py` — adds GPT target and utf-8 explicit file IO
- `scripts/check_agent_docs.py` — validates execution-plan scaffold and plan-template sections
- `scripts/llm_common.sh` — prompt builder now points providers at deeper docs instead of assuming `AGENTS.md` contains inline phase detail
- `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, `docs/INDEX.md`, `docs/core-beliefs.md`, `docs/operational-rules.md`, `docs/autofill-patterns.md`, `docs/board-architecture.md`, `docs/cli-reference.md`, `docs/PLAN_TEMPLATE.md` — normalized live docs and harness references
- `tests/test_ci_workflow.py`, `tests/test_llm_common.py` — parity and prompt-contract coverage

## Chunks & Tasks

### Chunk 1: Provider Copy Contract

#### Task 1: Expand generated provider surface

- [x] Add `GPT.md` to `sync_agent_files.py`
- [x] Regenerate all provider copies
- [x] Extend CI contract test to compare every generated provider file body with `AGENTS.md`

### Chunk 2: Runtime Prompt Contract

#### Task 2: Make prompt builders follow progressive disclosure

- [x] Add prompt guidance that treats `AGENTS.md` as the navigation map
- [x] Point drafting/research prompts at `docs/resume-generation.md`, `docs/cover-letter-generation.md`, `docs/shared-inputs.md`, and `agent_preferences.md`
- [x] Add prompt-text assertion in `tests/test_llm_common.py`

### Chunk 3: Harness Scaffolding

#### Task 3: Track and validate execution-plan infrastructure

- [x] Create `docs/exec-plans/active/` and `docs/exec-plans/completed/`
- [x] Add repo-level README for exec plans
- [x] Expand `docs/PLAN_TEMPLATE.md` with purpose, context, and milestone sections
- [x] Add `plans` validation to `scripts/check_agent_docs.py`

### Chunk 4: Live Documentation

#### Task 4: Remove stale active-doc wording

- [x] Update active docs to say `AGENTS.md` is the only editable instruction file
- [x] Replace stale `AGENTS.md` + `GEMINI.md` parity language with regenerate-all wording
- [x] Update active provider docs to include `openai` where runtime support already exists

## Surprises & Discoveries

- The repo already had a clean generated-copy model for Claude, Gemini, Codex, and Copilot, but no GPT/OpenAI-facing alias even though `openai` is a first-class runtime provider.
- The live provider prompt builders still used phrasing from the older inline-AGENTS era, which made the runtime contract lag behind the documentation refactor.
- The docs described execution plans as a standing pattern, but the tracked `docs/exec-plans/` directories themselves were still missing.

## Decision Log

- Decision: Add `GPT.md` as a generated alias instead of replacing `CODEX.md`.
  Rationale: `CODEX.md` is still the OpenAI Codex-specific entry point, while `GPT.md` satisfies the broader GPT-facing provider request without breaking existing references.
  Date/Author: 2026-03-30 / Codex
- Decision: Fix only live system-of-record docs, not historical plans/specs.
  Rationale: Historical artifacts should remain historically accurate; changing them would create noise without improving the active repo contract.
  Date/Author: 2026-03-30 / Codex
- Decision: Encode execution-plan scaffolding mechanically in `check_agent_docs.py`.
  Rationale: Harness guidance is most valuable when the repo can fail fast on drift instead of relying on memory.
  Date/Author: 2026-03-30 / Codex

## Outcomes & Retrospective

- **Achieved:** GPT-facing generated provider parity, runtime prompt progressive-disclosure alignment, tracked execution-plan directories, a stronger plan template, and normalized live docs/tests around the new contract.
- **Remaining:** If the team wants repo-owned local hook behavior, the next follow-up should track a shared hooks path instead of relying on an untracked local pre-commit hook.
- **Lessons:** The value in this repo is not more instruction text. It is a smaller canonical entry point, stronger links to deeper docs, and mechanical checks that keep the contract from drifting.
