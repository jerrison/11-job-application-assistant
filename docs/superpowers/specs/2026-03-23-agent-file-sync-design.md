# Agent File Sync & Preference Consolidation — Design Spec

**Goal:** Create a single-source agent instruction system with generated provider-specific files, consolidate Claude-only memory into a shared preferences file, and add automated validation to prevent drift.

**Date:** 2026-03-23

---

## Problem

1. **No CODEX.md exists.** Codex/GPT uses a condensed `.github/copilot-instructions.md` that's manually maintained and already stale.
2. **Preferences are Claude-only.** 25 memory files in `~/.claude/projects/.../memory/` contain form-filling defaults, workflow rules, and behavioral preferences that other providers (Gemini, Codex) never see.
3. **Sync is manual.** AGENTS.md and GEMINI.md are kept identical by CI diff, but `.github/copilot-instructions.md` drifts silently. No mechanism prevents any of the files from going stale.

## Design

### File Structure

```
AGENTS.md                              # Canonical source — full agent prompt (~162 lines)
GEMINI.md                              # GENERATED — Gemini header + AGENTS.md content
CODEX.md                               # GENERATED — Codex header + AGENTS.md content
.github/copilot-instructions.md        # GENERATED — condensed for GitHub Copilot
agent_preferences.md                   # NEW — consolidated behavioral preferences
scripts/sync_agent_files.py            # NEW — generator script
scripts/check_agent_docs.py            # NEW — doc-gardening validator
```

### 1. `agent_preferences.md`

Consolidates the 25 Claude memory files into a provider-agnostic shared file. Organized by section:

#### Sections

**Workflow Preferences**
- Always use `--draft`; never auto-submit unless user explicitly says "submit"
- "Draft" means full pipeline including autofill + screenshots, not just content generation
- Auto-retry transient failures; don't require manual requeuing
- Never re-send application just for Notion sync
- Auto-mark applied on LinkedIn after confirmed submission
- LinkedIn Easy Apply: never follow company, never mark job as priority
- Screenshots are source of truth for verifying autofill, not the autofill report JSON

**Form Filling Defaults**
- Workday auth flow: Sign in → password reset → create account; try "Apply Manually" first
- Combobox fields: click to expand, read all options, pick best match
- Culture/careers opt-in questions: always say Yes
- Bay Area location fields: use "San Francisco, CA"
- Compensation questions: never give numeric salary; deflect with "open and flexible"
- "How did you hear about us" priority: corporate website > company blog > LinkedIn > other

**Code Change Rules**
- Every fix must be generalized across ALL supported job boards and ALL runtime paths
- After 3+ similar per-board implementations, suggest a generic approach
- Update all provider instruction files (AGENTS.md, GEMINI.md, CODEX.md) when agent behavior changes
- Commit, push, merge after every individual fix — not batched

**Candidate Context (for agent tone/approach)**
- Speaks English, Spanish, Mandarin, Cantonese
- Employed full-time (not self-employed)
- Uses Claude Sonnet 4.6 as sole LLM provider at highest reasoning effort

**Working Style**
- Spawn subagents aggressively to minimize main context window usage
- Small logical commits; PRs must explain why and link to specs
- Use Playwright when WebFetch returns 403
- Use `gws` CLI for Google Docs and Gmail operations

#### AGENTS.md Integration

Add a single line to AGENTS.md in the "Shared Inputs" section:

```markdown
See [`agent_preferences.md`](agent_preferences.md) for behavioral defaults, form-filling
rules, and working style preferences. These apply to all providers and all runtime modes.
```

### 2. `scripts/sync_agent_files.py`

A Python script that generates provider-specific agent files from AGENTS.md.

#### Provider Headers

Each generated file gets a ~5-line header before the AGENTS.md content:

**GEMINI.md header:**
```markdown
<!-- GENERATED — do not edit. Source: AGENTS.md. Run: uv run python scripts/sync_agent_files.py -->
<!-- Provider: Google Gemini CLI. Tool mapping: Read→cat, Edit→patch, Bash→shell, Grep→grep -->
```

**CODEX.md header:**
```markdown
<!-- GENERATED — do not edit. Source: AGENTS.md. Run: uv run python scripts/sync_agent_files.py -->
<!-- Provider: OpenAI Codex CLI. Tool mapping: Read→cat, Edit→patch, Bash→shell, Grep→grep -->
```

**.github/copilot-instructions.md:**
Generated as a condensed version (~40 lines): project overview, key conventions, runtime commands, and a pointer to AGENTS.md for full instructions.

#### CLI Interface

```bash
# Generate all provider files
uv run python scripts/sync_agent_files.py

# Check mode — exit 1 if any file is stale (for CI/hooks)
uv run python scripts/sync_agent_files.py --check

# Generate a specific file only
uv run python scripts/sync_agent_files.py --target gemini
```

### 3. `scripts/check_agent_docs.py`

Doc-gardening validator that checks for drift and staleness.

#### Checks Performed

1. **Agent file sync** — runs `sync_agent_files.py --check` internally
2. **Preference coverage** — compares `agent_preferences.md` sections against Claude memory files in `~/.claude/projects/.../memory/`. Flags memory entries that aren't reflected in the shared file.
3. **Broken links** — scans all `.md` files in the repo root and `docs/` for internal links (`[text](path)`) and verifies the targets exist.
4. **AGENTS.md size** — warns if AGENTS.md exceeds 200 lines (progressive disclosure principle from OpenAI harness article).
5. **Cross-reference validation** — verifies files referenced in `docs/INDEX.md` exist.

#### CLI Interface

```bash
# Run all checks
uv run python scripts/check_agent_docs.py

# Run specific check
uv run python scripts/check_agent_docs.py --check links
uv run python scripts/check_agent_docs.py --check preferences
uv run python scripts/check_agent_docs.py --check sync
```

#### Output Format

```
✓ Agent files in sync (GEMINI.md, CODEX.md, copilot-instructions.md)
✓ AGENTS.md is 162 lines (under 200 limit)
✗ Preference drift: memory file 'feedback_use_playwright_for_403.md' not in agent_preferences.md
✓ All 47 internal doc links resolve
✓ All 12 INDEX.md references exist
```

### 4. Integration Points

#### CI

Add to existing CI workflow (or pre-commit hook):
```yaml
- name: Validate agent docs
  run: uv run python scripts/sync_agent_files.py --check
```

#### Post-Fix Workflow

`docs/operational-rules.md` already says "update all provider instructions." The sync script makes this mechanical: edit AGENTS.md, run `sync_agent_files.py`, commit all generated files together.

#### Claude Memory Maintenance

Claude's memory files continue to exist for Claude-specific session behavior. But any preference that affects agent behavior across providers gets added to `agent_preferences.md` instead of (or in addition to) Claude memory. The `check_agent_docs.py` script flags drift.

### 5. What Does NOT Change

- **AGENTS.md content** — no changes to the actual agent prompt
- **`application_profile.md`** — form autofill defaults (name, email, EEO answers) stay here
- **`candidate_context.md`** — narrative background for cover letters stays here
- **`master_resume.md`** / **`work_stories.md`** — source material stays here
- **`docs/` structure** — all existing docs remain as-is
- **LLM prompt functions in `llm_common.sh`** — no changes needed; they already reference `application_profile.md` and `candidate_context.md`

### 6. Design Principles (from OpenAI Harness Article)

- **AGENTS.md as table of contents** — keep it lean (~100-200 lines), point to detailed docs
- **Repo is system of record** — preferences encoded in versioned files, not external tools
- **Enforce invariants mechanically** — CI/hooks validate sync, not humans remembering
- **Progressive disclosure** — agents read AGENTS.md first, follow links only when needed
- **Doc-gardening as process** — automated checks prevent rot

---

## Out of Scope

- Provider-specific prompt customization (all providers get identical instructions for now)
- Automated doc-gardening PR creation (manual for now; script reports issues)
- Changes to the LLM prompt pipeline (`llm_common.sh`, `apply.sh`)
- Migrating away from Claude memory entirely (it still serves session-specific purposes)
