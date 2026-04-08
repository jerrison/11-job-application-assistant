# Prompt Registry

Version prompt-like control surfaces as if they were code. This registry makes
ownership, change control, and rollback paths explicit.

| Artifact | Source of truth | Purpose | Change control | Rollback / verification |
|----------|-----------------|---------|----------------|-------------------------|
| `AGENTS.md` | `AGENTS.md` | Canonical repo instructions and operator contract | Edit this file directly, then regenerate provider copies | `uv run python scripts/sync_agent_files.py --check`, `uv run python scripts/check_agent_docs.py`, revert the commit if needed |
| Provider instruction copies | `CLAUDE.md`, `GEMINI.md`, `CODEX.md`, `GPT.md`, `.github/copilot-instructions.md` | Generated mirrors of `AGENTS.md` for provider-specific runtimes | Never edit directly; regenerate from `AGENTS.md` | Re-run sync and check scripts |
| `agent_preferences.md` | `agent_preferences.md` | Learned behavioral defaults and corrections | Update in the same change as the behavior it governs | Validate against affected tests and runtime smoke checks |
| `docs/core-beliefs.md` | `docs/core-beliefs.md` | Golden principles for agent legibility and repo coherence | Update when invariants or repo conventions change | Review diff, run docs + verification checks, revert commit if the rule set is wrong |
| `docs/operational-rules.md` | `docs/operational-rules.md` | Runtime operating rules and approval boundaries | Update alongside behavior or authority changes | Same as above; ensure generated instructions still point to the right docs |
| `governance/runtime-policy.json` | `governance/runtime-policy.json` | Policy-as-code action tiers and explicit-approval gates for runtime operations | Update in the same change as any new governed action or approval rule | Re-run runtime policy tests and affected surface tests; revert the policy change if the gate is wrong |
| `docs/backlog-sweep.md` and `docs/runbooks/*` | These files directly | Sweep-specific runbooks and machine-checkable completion rules | Update with recorder/verifier changes | Re-run the relevant checker/verifier before merge |
| `docs/exec-plans/active/*` | Active execution plan files | Current long-running task plan, progress, and decision log | Keep current while work is in flight | Review plan progress and outcomes; move to `completed/` when done |
| `docs/exec-plans/completed/*` | Completed execution plan files | Historical audit log of complex work | Append new completed plans; do not silently rewrite history | Revert via git if a completed record is wrong |

If a new instruction surface or reusable prompt source is added, register it
here in the same change.
