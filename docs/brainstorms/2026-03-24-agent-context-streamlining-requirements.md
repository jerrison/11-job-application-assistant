---
date: 2026-03-24
topic: agent-context-streamlining
---

# Agent Context Streamlining & Provider Parity

## Problem Frame

Agent instructions are bloated (AGENTS.md at 167 lines), memory is Claude-only (29 files invisible to GPT/Gemini), and provider-specific files drift in coverage (copilot-instructions.md is ~40 lines vs ~170 for others). This means:
- Agents waste context budget on instructions they don't need for the current task
- GPT and Gemini sessions start with zero learned preferences, re-discovering rules each time
- Fixes applied in one provider's context don't propagate to others
- Shared writing preferences can drift by provider, causing inconsistent punctuation across generated cover-letter text and application answers


## Requirements

### Context Reduction

- R1. **AGENTS.md becomes a ~80-100 line navigation map.** Project overview, dev commands, critical cross-cutting rules, and pointers to docs/ — including all preference and memory file locations so any provider knows where to look for learned rules. No inlined content that duplicates existing docs/ files.

### Provider Parity

- R2. **All provider files are generated copies of AGENTS.md.** CLAUDE.md, GEMINI.md, CODEX.md, and .github/copilot-instructions.md are all generated from AGENTS.md via `sync_agent_files.py`. Full content, not condensed. The custom copilot builder is removed in favor of the same full-copy pattern.
- R3. **Sync script updated.** `sync_agent_files.py` adds CLAUDE.md as a sync target and removes the condensed copilot builder. Pre-commit hook updated to stage CLAUDE.md.

### Memory Portability

- R4. **Cross-provider knowledge absorbed into repo files.** Memory items that are application domain knowledge (form-filling rules, board behaviors, candidate context, workflow preferences) are merged into their natural homes: `agent_preferences.md`, `docs/autofill-patterns.md`, `docs/operational-rules.md`, etc.
- R5. **Claude memory trimmed to Claude-specific behavioral preferences only.** Items like "spawn subagents aggressively" and "minimize context" stay in Claude memory. Everything else moves to repo-local files that all providers read.

### Shared Writing Behavior

- R6. **Cross-provider prose style rules are shared.** Writing preferences that affect generated cover-letter text and generated application answers must live in repo-local instructions that all providers read, rather than staying provider-specific.
- R7. **Em dash avoidance is a default for cover-letter text and application answers.** Providers should avoid em dashes when possible in generated cover letters and generated application answers. Direct quotes and fixed text may preserve the original punctuation.

### Harness Engineering Practices

- R8. **ARCHITECTURE.md added.** ~50-line codemap showing module relationships, layers (scripts/ → pipeline → autofill → boards), key invariants, and deliberate absences of dependencies.
- R9. **Execution plans directory.** `docs/exec-plans/active/` and `docs/exec-plans/completed/` for complex multi-step tasks. Use existing `docs/PLAN_TEMPLATE.md` as the template. Self-contained, verifiable milestones, decision logs.
- R10. **Remediation lint messages.** `check_architecture.py` error messages include agent-facing fix instructions (e.g., "Move this import to the service layer: scripts/services/").

## Success Criteria

- AGENTS.md is ≤100 lines and serves as a TOC
- All four generated files (CLAUDE.md, GEMINI.md, CODEX.md, .github/copilot-instructions.md) are identical copies of AGENTS.md
- GPT/Gemini agents can access all learned preferences by reading repo files referenced in AGENTS.md
- Claude memory contains only Claude-behavioral items (e.g., subagent preferences, response style)
- All providers apply the same em dash avoidance rule to generated cover-letter text and application answers
- Direct quotes and fixed text preserve original punctuation when needed
- ARCHITECTURE.md exists and accurately reflects module structure
- `docs/exec-plans/` directory structure exists
- `check_architecture.py` violations include remediation instructions
- `sync_agent_files.py --check` passes in CI after all changes
- Pre-commit hook still auto-syncs on AGENTS.md changes

## Scope Boundaries

- **Not changing the pipeline or autofill code** — this is purely agent context and tooling
- **Not building a custom memory system** — leveraging existing repo files as the knowledge store
- **Not changing CI workflows** — existing `--check` enforcement is sufficient
- **Not automating entropy sweeps yet** — that's a follow-up after the foundation is solid
- **Not retroactively rewriting existing repo docs or source materials for punctuation** — this only governs future generated cover-letter text and application answers
- **Not applying prose-style rules to filenames, resume content, source code, or copied user text** — unless a later brainstorm explicitly expands the scope

## Key Decisions

- **Repo-local over provider-specific memory:** All providers read the same files from the repo. No provider-specific memory stores except Claude's built-in (for Claude-behavioral items only).
- **Single entry point:** AGENTS.md is the universal entry point. CLAUDE.md, GEMINI.md, CODEX.md, and .github/copilot-instructions.md are all generated copies. Aggressively trimmed to ~80-100 lines as a TOC.
- **Progressive disclosure:** Agents read AGENTS.md first (TOC), then fetch deeper docs/ files on-demand based on the task. Board-specific patterns, form-filling defaults, and architecture details stay in docs/.
- **Absorb, don't duplicate:** Memory items merge into existing docs rather than creating a new docs/agent-memory/ directory.
- **Style rules follow the same parity model:** If a writing preference affects generated cover-letter text or generated application answers across providers, it belongs in shared repo instructions.
- **Em dash rule is a soft normalization default, not a blanket rewrite:** Avoid em dashes in assistant-authored prose when possible, but preserve them for direct quotes and fixed text.

## Dependencies / Assumptions

- Claude Code's built-in memory system continues to use `~/.claude/projects/` path — we can't redirect it, so we trim it rather than move it
- Pre-commit hook and CI sync check remain the enforcement mechanism

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Technical] What specific content from current AGENTS.md should remain inline vs move to docs/?
- [Affects R4][Needs research] Which of the 29 memory files map to which existing docs/ files? Need to audit each one.
- [Affects R8][Technical] What's the accurate module dependency graph for ARCHITECTURE.md? Needs codebase exploration.
- [Affects R10][Technical] What's the current check_architecture.py violation format, and what remediation patterns should be injected?

## Next Steps

→ `/ce:plan` for structured implementation planning
