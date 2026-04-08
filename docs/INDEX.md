# Docs Index

Navigation hub for agents. Read the 1-line description for each doc; only open a file when its topic is relevant to your current task.

## Start Here

| File | What it tells you |
|------|-------------------|
| [`AGENTS.md`](../AGENTS.md) | Canonical agent prompt and navigation map shared across all providers |
| [`CLAUDE.md`](../CLAUDE.md) | Generated from AGENTS.md — Claude Code entry point |
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | System architecture overview with invariant callouts |
| [`agent_preferences.md`](../agent_preferences.md) | Behavioral defaults, form-filling rules, working style (all providers) |
| [`CODEX.md`](../CODEX.md) | Generated from AGENTS.md for OpenAI Codex CLI |
| [`GPT.md`](../GPT.md) | Generated from AGENTS.md for GPT-oriented OpenAI runtimes |
| [`GEMINI.md`](../GEMINI.md) | Generated from AGENTS.md for Google Gemini CLI |

## Architecture

| Doc | Covers | Read when... |
|-----|--------|--------------|
| [`board-architecture.md`](board-architecture.md) | Composition-based autofill architecture, question classifier, provider chain, TUI/worker/queue | Building or debugging any board autofill script |
| [`autofill-patterns.md`](autofill-patterns.md) | Board-specific runtime gotchas and implementation rules (all boards) | Hitting a board-specific bug or adding a new board |
| [`worker-pipeline-patterns.md`](worker-pipeline-patterns.md) | Status flow, auto-retry, rate limiting, browser profiles, captcha, LinkedIn import | Debugging worker behavior or pipeline failures |

## Workflows

| Doc | Covers | Read when... |
|-----|--------|--------------|
| [`resume-generation.md`](resume-generation.md) | Full 5-phase resume pipeline: parse JD, rank bullets, draft, review, render | Modifying resume tailoring or debugging output quality |
| [`cover-letter-generation.md`](cover-letter-generation.md) | Full 5-phase cover letter pipeline: research, outline, draft, review, render | Modifying cover letter generation or tone |

## Reference

| Doc | Covers | Read when... |
|-----|--------|--------------|
| [`cli-reference.md`](cli-reference.md) | CLI commands, provider guidance, batch/parallel, submit, interview prep | Running or modifying CLI entrypoints |
| [`shared-inputs.md`](shared-inputs.md) | Required inputs per session: JD scraping, templates, source files, profile | Understanding what inputs the pipeline needs |
| [`launch-modes.md`](launch-modes.md) | Single-job and batch-from-Notion workflows, Notion DB schema | Processing jobs via CLI or Notion |
| [`output-structure.md`](output-structure.md) | Output directory layout, file naming conventions | Looking for output files or adding new artifacts |

## Operations

| Doc | Covers | Read when... |
|-----|--------|--------------|
| [`operational-rules.md`](operational-rules.md) | Standing orders, post-fix workflow, commit/merge/push policy | Starting any task (skim once) |
| [`backlog-sweep.md`](backlog-sweep.md) | Snapshot/ledger contract and completion gate for large stopped/draft sweeps | Running a queue sweep that must exhaust every snapshot row |
| [`runbooks/repeatable-backlog-sweep.md`](runbooks/repeatable-backlog-sweep.md) | Versioned operator runbook plus the exact reusable backlog-sweep prompt | Re-running the full Todo/stopped/draft sweep against current repo and queue state |
| [`core-beliefs.md`](core-beliefs.md) | Golden principles for agent-first development | Proposing architectural changes or new conventions |
| [`QUALITY_SCORE.md`](QUALITY_SCORE.md) | Per-domain quality grading rubric | Evaluating or improving output quality |

## Investigations

| Doc | Covers | Read when... |
|-----|--------|--------------|
| [`docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`](solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md) | Repo-local stopped-job audit snapshot, failure-cluster evidence, and the rule that external trackers are mirrors only | Resuming stopped-job troubleshooting or validating the current audit baseline |
| [`docs/solutions/workflow-issues/workday-draft-reruns-must-fail-closed-before-submit-2026-03-28.md`](solutions/workflow-issues/workday-draft-reruns-must-fail-closed-before-submit-2026-03-28.md) | Autodesk incident write-up plus the Workday fail-closed draft guard that prevents review-page submit clicks during `--draft` reruns | Touching Workday draft-mode safety, review-shell detection, or submit-boundary logic |
| [`docs/solutions/workflow-issues/2026-03-29-screenshot-batch-needed-cross-board-proof-and-opt-out-fixes.md`](solutions/workflow-issues/2026-03-29-screenshot-batch-needed-cross-board-proof-and-opt-out-fixes.md) | 2026-03-29 screenshot-batch write-up covering duplicate normalization, Greenhouse proof capture, LinkedIn follow opt-outs, deterministic anti-AI answers, and Workday resume dedupe | Replaying the 2026-03-29 batch or touching any of the same cross-board proof / opt-out failure modes |

## Agent File Sync

| Tool | Purpose | When to use |
|------|---------|-------------|
| `scripts/sync_agent_files.py` | Generate CLAUDE.md, GEMINI.md, CODEX.md, GPT.md, and copilot-instructions.md from AGENTS.md | After editing AGENTS.md |
| `scripts/sync_agent_files.py --check` | Verify generated files are up to date (used in CI) | Debugging CI failures |
| `scripts/check_agent_docs.py` | Validate doc links, AGENTS.md size, INDEX.md refs, agent file sync, and execution-plan scaffolding | Periodic doc gardening |

## Source Material (repo root)

| File | Purpose |
|------|---------|
| Runtime `master_resume.md` | User-owned bullet pool -- source of truth for resume tailoring; see [`shared-inputs.md`](shared-inputs.md) |
| Runtime `work_stories.md` | User-owned narrative color for cover letter generation; see [`shared-inputs.md`](shared-inputs.md) |
| Runtime `candidate_context.md` | User-owned supplemental candidate background and preferences; see [`shared-inputs.md`](shared-inputs.md) |
| Runtime `application_profile.md` | User-owned form field defaults for autofill (name, links, EEO); see [`shared-inputs.md`](shared-inputs.md) |

## Key Scripts

| Script | Role | Read when... |
|--------|------|--------------|
| `scripts/question_classifier.py` | Unified question classifier — priority-ordered detector dispatch, maps labels to categories + deterministic answers | Adding a new deterministic question type or debugging question routing |
| `scripts/application_submit_common.py` | Individual detector functions, profile parsing, LLM answer generation, Gmail/Notion sync | Modifying detector logic or shared submit utilities |
| `scripts/print_backlog_sweep_handoff.py` | Prints a ready-to-paste prompt for resuming the active backlog sweep from the current manifest and ledgers | Handing an in-flight sweep to a fresh agent session |

## Planning & Execution

| Directory | Contents | When to use |
|-----------|----------|-------------|
| `exec-plans/active/` | Living execution plans with progress and decision logs for in-flight multi-step work | When a change needs a maintained execution record while work is underway |
| `exec-plans/completed/` | Archived execution plans moved out of the active queue after landing | When you need historical implementation context for a finished change |
| [`PLAN_TEMPLATE.md`](PLAN_TEMPLATE.md) | Template for new execution plans, including progress, discoveries, decision log, and retrospective sections | Before starting a complex multi-file change |
| `superpowers/plans/` | Historical planning artifacts produced by earlier planning workflows | When tracing prior implementation plans |
| `superpowers/specs/` | Historical design specifications | When an existing design/spec already covers the area you are touching |

**Naming convention:** `YYYY-MM-DD-<slug>.md` for plans, `YYYY-MM-DD-<slug>-design.md` for specs.
