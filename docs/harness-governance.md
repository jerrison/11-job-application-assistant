# Harness & Governance

This document turns long-running agent guidance into repo-local operating rules.
The goal is simple: a fresh agent or reviewer should be able to understand the
current state, the authority boundaries, and the proof of work from repository
artifacts alone.

## Operating Model

- Humans steer. Agents execute.
- The repo is durable memory. Plans, runbooks, status notes, screenshots,
  result JSON, and git history must be enough for a fresh context to resume.
- Multi-step work is checkpointed. Each milestone needs clear constraints,
  validation commands, and a stop-and-fix rule before the next milestone.
- Prefer specialized surfaces over one giant agent. CLI, web, TUI, workers,
  and draft review share the same backend contract but keep distinct runtime
  responsibilities.
- Hidden chat state is not a source of truth. If it matters later, write it
  into the repo or the per-run artifacts.

## Durable Artifact Stack

The default artifact stack for long-running work in this repo is:

1. `AGENTS.md`, `agent_preferences.md`, and `docs/core-beliefs.md`
   for standing behavior and invariants.
2. `docs/exec-plans/active/*.md` for the current multi-step task.
3. `docs/operational-rules.md`, `docs/backlog-sweep.md`, and
   `docs/runbooks/*` for surface-specific runbooks.
4. Per-job proof in `output/<company>/<role>/submit/` and related structured
   runtime artifacts.
5. Git history for change lineage and rollback.

If a task spans multiple sessions, approvals, or handoffs, the active plan must
stay current enough that another agent can resume from the repo alone.

## Shared Settings And User Context

All operator surfaces must resolve the same user-owned materials and
credentials through `scripts/app_paths.py` and `scripts/settings_store.py`.
No surface may introduce a shadow copy of user context or a separate settings
schema without updating this contract.

Canonical user context files:

- `master_resume.md`
- `work_stories.md`
- `candidate_context.md`
- `application_profile.md`

Required onboarding capabilities across the packaged app, web app, CLI, and
TUI:

- paste text or markdown directly
- import `.txt`, `.md`, or `.docx`
- import from a public Google Drive or Google Docs link
- configure provider credentials such as OpenAI, Gemini, Codex, Anthropic,
  and Steel

These files and credentials are runtime state. They must never be committed,
and UI/CLI layers must edit them through the shared settings backend rather
than by inventing surface-specific flows.

## Risk Tiers And Approval Boundaries

| Tier | Examples | Required controls | Approval authority |
|------|----------|-------------------|--------------------|
| `L0` | Repo-local docs, tests, refactors, local verification | Keep diffs scoped, run verification, update docs when behavior changes | Agent may act locally |
| `L1` | Importing user materials, editing local settings, storing API keys, fetching public onboarding URLs | Operator-initiated only, no secret echoing, no committed runtime data | Operator initiates; agent may complete the requested local change |
| `L2` | Draft generation, draft-mode autofill, browser automation before submit | Fail closed, keep screenshot/result proof, stop on ambiguous submit state | User request to draft/apply is sufficient |
| `L3` | Live submission, destructive deletes, push, merge, public release | Explicit boundary check, completed verification, inspectable proof | Explicit user approval required |

The default application workflow in this repo is `L2`. The system should stop
before crossing into an `L3` action unless the operator explicitly approves it.

## Registries

Maintain these registries as first-class governance artifacts:

- [`registries/agent-registry.md`](registries/agent-registry.md)
- [`registries/tool-registry.md`](registries/tool-registry.md)
- [`registries/prompt-registry.md`](registries/prompt-registry.md)

If a change adds a new long-running surface, new authority, or a new external
integration, update the relevant registry in the same change.

## Observability, Audit, And Data Minimization

- Externalize state. The current milestone, next step, and recent decisions
  should live in repo artifacts, not only in chat.
- Keep proof close to the work. UI/browser decisions need screenshots or
  structured results. Pipeline decisions need logs or JSON artifacts.
- Preserve auditability without oversharing. Secrets, personal URLs, and
  unnecessary PII should be redacted or omitted from logs, docs, and commits.
- When remote tracing would capture sensitive data, prefer local structured
  logs or redacted processors over raw third-party traces.
- Startup surfaces should register trace processors explicitly rather than
  relying on ad hoc per-call defaults. Governance needs visible wiring, not
  hidden side effects.

## Release Gate Before Push Or Merge

Do not push, merge, or publish from this repo unless all of the following are
true:

- Relevant verification commands pass.
- The diff contains no runtime files, secrets, personal URLs, or generated
  applicant artifacts.
- The affected governance docs and registries are updated when authority,
  tooling, or behavior changed.
- Any `L3` boundary has explicit operator approval.
- A reviewer can understand what changed, why it changed, and how it was
  verified from the repo alone.
