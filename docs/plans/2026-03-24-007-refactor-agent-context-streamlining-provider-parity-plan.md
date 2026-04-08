---
title: "refactor: Agent context streamlining & provider parity"
type: refactor
status: active
date: 2026-03-24
origin: docs/brainstorms/2026-03-24-agent-context-streamlining-requirements.md
deepened: 2026-03-26
---

# refactor: Agent Context Streamlining & Provider Parity

## Overview

The original provider-parity work trimmed AGENTS.md to a navigation TOC, made all four provider files generated copies, absorbed Claude-only memory into repo files, added ARCHITECTURE.md, and expanded lint enforcement with remediation messages.

This plan is reopened for a narrow follow-up: codify a shared prose-style rule across every LLM provider surface that writes generated cover-letter body text or generated application answers. The immediate requirement is em dash avoidance in those outputs, while preserving direct quotes and fixed text.

## Current State

The broader provider-parity refactor is already present in the repo and is not the active work for this follow-up.

- `AGENTS.md` is the canonical instruction file.
- `CLAUDE.md`, `GEMINI.md`, `CODEX.md`, and `.github/copilot-instructions.md` are generated from `AGENTS.md` via `scripts/sync_agent_files.py`.
- `agent_preferences.md` is the canonical repo-local home for shared learned behavior.
- Cover-letter drafting flows in `scripts/llm_common.sh` already instruct providers to follow `AGENTS.md`, but the cover-letter body guidance is not yet mirrored inline.
- Generated application answers bypass `AGENTS.md` at runtime and are built inline in `scripts/application_submit_common.py` and `scripts/autofill_greenhouse.py`.
- The repo does not yet encode the narrowed em dash rule in shared instructions, the cover-letter drafting prompt, or the answer-generation prompt builders.

## Problem Frame

Provider file parity solved instruction distribution, but not every target content-generation surface reads the same text at runtime.

- If the rule lives only in `agent_preferences.md`, provider sessions may miss it.
- If the rule lives only in `AGENTS.md`, generated cover-letter text and generated application answers can still drift because their prompt builders own the final runtime instructions.
- The follow-up must add one canonical rule and mirror it only where the runtime cannot rely on `AGENTS.md` alone.

## Requirements Trace

- R6. Cross-provider prose-style rules live in shared repo instructions (see origin).
- R7. Em dash avoidance is the default for assistant-authored prose, with exceptions for direct quotes and fixed text (see origin).
- S1. The rule applies consistently to generated cover-letter body text and generated application answers.
- S2. The active change remains narrow and does not reopen the earlier provider-parity refactor.

## Scope Boundaries

- In scope: generated cover-letter body text and generated application answers.
- Out of scope: chat responses, filenames, resume content, research caches, internal analysis artifacts, code, comments, copied user text, raw job-post excerpts preserved verbatim, and unrelated provider-parity hardening work.
- Direct quote: verbatim user-provided or source-provided text intentionally preserved as a quote.
- Fixed text: externally owned or previously authored strings copied verbatim rather than rewritten, such as stored resume bullets, literal error messages, or job-posting excerpts.
- Character scope: this is a soft preference about the Unicode em dash character `—`. It does not ban ASCII hyphens or literal `--` in code, CLI text, or copied source text.

## Context & Research

### Relevant Code and Patterns

- `AGENTS.md` plus generated provider copies via `scripts/sync_agent_files.py`
- `agent_preferences.md` as the canonical home for learned cross-provider defaults
- `job_assets_write_drafting_prompt()` in `scripts/llm_common.sh` generates the runtime drafting instructions that cover both resume and cover-letter creation, so the cover-letter body rule should be mirrored there explicitly with cover-letter-only scope
- `build_application_answers_prompt()` in `scripts/application_submit_common.py` and `_build_application_answers_prompt()` in `scripts/autofill_greenhouse.py` are the known prose surfaces that bypass `AGENTS.md` at runtime

### Institutional Learnings

- `docs/solutions/integration-issues/adding-new-llm-provider.md`: keep a single source of truth and avoid parallel instruction copies that drift

### External References

- OpenAI harness engineering: progressive disclosure and mechanical enforcement for shared instructions

## Key Technical Decisions

- Canonical wording lives in `agent_preferences.md`.
- `AGENTS.md` carries a concise version of the rule because provider sessions and generated copies read it first.
- `scripts/llm_common.sh` gets a cover-letter-only mirrored sentence because the drafting prompt owns the final runtime instructions for generated cover-letter body text.
- Generated application-answer prompt builders get a short mirrored sentence because they bypass `AGENTS.md` at runtime.
- Earlier provider-parity work is historical baseline, not reopened scope.
- This follow-up does not change hook behavior, AGENTS ordering, line-limit policy, shared-input migration, or ARCHITECTURE maintenance.

## Open Questions

### Resolved During Planning

- The active goal is only the em dash rule, not the earlier provider-parity hardening work.
- The rule applies only to generated cover-letter body text and generated application answers, not filenames, resume content, research caches, or code.
- The rule is a soft preference, not a hard validation ban.

### Deferred to Implementation

- The exact mirrored sentence in the two answer-generation prompt builders can vary slightly in wording as long as it preserves the same scope and exceptions.

## Implementation Units

- [ ] **Unit 1: Encode the Rule in Canonical Instruction Surfaces**

**Goal:** Put the em dash rule where interactive providers will see it and where future corrections belong.

**Requirements:** R6, R7, S1, S2

**Dependencies:** Existing sync pipeline remains unchanged

**Files:**
- Modify: `agent_preferences.md`
- Modify: `AGENTS.md`
- Generated: `CLAUDE.md`
- Generated: `GEMINI.md`
- Generated: `CODEX.md`
- Generated: `.github/copilot-instructions.md`

**Approach:**
- Add the normative rule to `agent_preferences.md` with explicit scope, exception definitions, and `—`-character specificity.
- Add a concise summary line to `AGENTS.md` in the shared cross-provider behavior section.
- Regenerate provider copies via `scripts/sync_agent_files.py` rather than editing them directly.
- Keep the current AGENTS structure intact. This follow-up does not reorder the document.

**Patterns to follow:**
- Existing sync model in `scripts/sync_agent_files.py`
- Existing AGENTS pointer to `agent_preferences.md`

**Test scenarios:**
- Interactive provider docs all show the same concise rule after sync.
- The canonical preference text distinguishes direct quotes and fixed text from targeted generated outputs.

**Verification:**
- `uv run python scripts/sync_agent_files.py --check` passes.
- Generated provider files reflect the AGENTS update without direct edits.

- [ ] **Unit 2: Bring Generated Cover-Letter and Application-Answer Prompts into Parity**

**Goal:** Ensure generated cover-letter body text and generated application answers follow the same rule even though their runtime prompts can diverge from `AGENTS.md`.

**Requirements:** R7, S1, S2

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/llm_common.sh`
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/autofill_greenhouse.py`
- Test: `tests/test_llm_common.py`
- Test: `tests/test_submit_application.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Add a cover-letter-only version of the rule to the drafting prompt in `scripts/llm_common.sh`.
- Add the same short application-answer rule to both answer-generation instruction lists.
- Keep the mirrored text minimal and semantically aligned with the AGENTS and `agent_preferences.md` rule.
- Do not expand scope to research prompts or unrelated prompt builders.
- Add explicit prompt-text assertions in the three affected test files instead of relying on existing functional coverage implicitly.

**Patterns to follow:**
- Existing heredoc prompt pattern in `scripts/llm_common.sh`
- Existing inline instruction-list pattern in both builders
- Existing shell prompt assertions in `tests/test_llm_common.py`
- Existing prompt-content assertions in `tests/test_submit_application.py`
- Existing prompt-content assertions in `tests/test_greenhouse_autofill.py`

**Test scenarios:**
- The drafting prompt contains cover-letter-only em dash guidance.
- The default application-answer prompt contains both the em dash guidance and the direct-quote/fixed-text exception.
- The Greenhouse prompt contains the same application-answer rule.
- Existing answer-generation behavior remains otherwise unchanged.

**Verification:**
- Tests assert the new prompt text directly with substring checks.
- Existing answer-generation tests continue to pass without provider-specific branching.

## System-Wide Impact

- **Interaction graph:** AGENTS sync affects interactive provider docs. `scripts/llm_common.sh` affects generated cover-letter body text. The answer prompt builders affect all boards using `generate_application_answers()` plus Greenhouse's separate path.
- **Error propagation:** No runtime control-flow changes. The primary risk is instruction drift.
- **API surface parity:** Only the instruction surfaces that actually emit assistant-authored prose are affected in this follow-up.
- **Integration coverage:** Prompt-content assertions are required because functional tests alone will not prove wording parity.

## Risks & Dependencies

- Risk: rule drift between canonical docs and inline cover-letter or answer prompts.
  Mitigation: keep canonical wording in `agent_preferences.md`, keep each mirrored prompt text to a single sentence, and cover them with prompt-text assertions.
- Risk: active scope expands back into the earlier provider-parity refactor.
  Mitigation: keep acceptance criteria limited to Units 1 and 2 only.
- Risk: research outputs later get reused as final copy and reintroduce the character.
  Mitigation: keep research outputs explicitly out of scope for this follow-up. Revisit only if a later bug shows leakage into final user-facing assets.

## Acceptance Criteria

- [ ] `agent_preferences.md` records the em dash avoidance default for generated cover-letter body text and generated application answers, with direct-quote and fixed-text exceptions.
- [ ] `AGENTS.md` carries a concise version of the rule and regenerated provider copies stay in sync.
- [ ] `scripts/llm_common.sh` includes the mirrored cover-letter-body rule in the drafting prompt.
- [ ] `scripts/application_submit_common.py` and `scripts/autofill_greenhouse.py` include the mirrored em dash rule in their answer-generation prompt builders.
- [ ] `tests/test_llm_common.py`, `tests/test_submit_application.py`, and `tests/test_greenhouse_autofill.py` assert the new prompt text directly.
- [ ] The active plan does not reopen hook behavior, AGENTS reordering, AGENTS trimming, shared-input migration, or harness-architecture changes.

## Documentation / Operational Notes

- No hook changes, ARCHITECTURE changes, or doc-migration work are part of this follow-up.
- If more prose-style rules accumulate later, revisit whether prompt-fragment reuse is warranted.

## Historical Baseline

The broader provider-parity refactor already landed in repo. This file continues to reference that work as context, but execution from this point should treat only Unit 1 and Unit 2 as active.

## Sources & References

### Origin

- `docs/brainstorms/2026-03-24-agent-context-streamlining-requirements.md`

### Internal References

- `AGENTS.md`
- `agent_preferences.md`
- `scripts/sync_agent_files.py`
- `scripts/application_submit_common.py`
- `scripts/autofill_greenhouse.py`
- `tests/test_submit_application.py`
- `tests/test_greenhouse_autofill.py`
- `docs/solutions/integration-issues/adding-new-llm-provider.md`

### External References

- OpenAI harness engineering
