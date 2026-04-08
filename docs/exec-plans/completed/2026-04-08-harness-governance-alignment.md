# Harness Governance Alignment Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the public app repo with durable-memory, approval-boundary, and registry principles before any push or merge.

**Spec:** User request plus the repo-local harness/governance docs added in this change.

**Existing code:** `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, `docs/core-beliefs.md`, `docs/operational-rules.md`, `docs/shared-inputs.md`, `docs/provider-setup.md`, `docs/INDEX.md`

---

## Purpose / Big Picture

After this change, a reviewer can see the repo's authority boundaries,
durable-memory expectations, release gate, and shared settings contract
directly from checked-in docs instead of inferring them from chat history.

---

## Context and Orientation

- **Docs to read:** `docs/harness-governance.md`, `docs/registries/*.md`, `docs/shared-inputs.md`
- **Primary files:** `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, `docs/operational-rules.md`
- **Constraints:** Do not push or merge anything without explicit user approval. Keep runtime data and secrets out of git.

---

## Milestones

1. **Governance contract:** Add a repo-local harness/governance doc with risk tiers and release gates.
2. **Registry visibility:** Add explicit agent, tool, and prompt registries.
3. **Surface parity docs:** Update top-level docs so shared settings/onboarding is the contract across app, web, CLI, and TUI.

---

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Governance doc added | Complete | 2026-04-08 |
| Registries added | Complete | 2026-04-08 |
| Top-level docs updated | Complete | 2026-04-08 |
| Verification rerun | Pending | |

---

## Surprises & Discoveries

- The new repo already had strong plan/runbook patterns; the main missing pieces were explicit registries, risk tiers, and publication boundaries.
- `docs/operational-rules.md` still implied automatic push/merge after every fix and needed to be softened into an approval gate.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-08 | Add explicit registry docs instead of only describing registries abstractly | The governance guidance calls for first-class visibility into agents, tools, and prompt/control surfaces |
| 2026-04-08 | Keep the shared settings contract in docs rather than adding a new enforcement script in this pass | The backend already exists in code; the immediate gap was missing repo-level documentation and approval boundaries |

---

## Outcomes & Retrospective

- **Achieved:** Added a repo-local harness/governance contract, explicit registries, and updated top-level docs to reflect shared settings parity and push/merge approval gates.
- **Remaining:** Packaged app distribution and any future enforcement scripts or policy-as-code checks can build on these docs.
- **Lessons:** The repo already had good long-running-task mechanics; making the authority model explicit was the highest-leverage gap to close before publication.
