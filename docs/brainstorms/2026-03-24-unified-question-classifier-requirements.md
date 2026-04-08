---
date: 2026-03-24
topic: unified-question-classifier
---

# Unified Question Classifier — Prevent Autofill Regressions

## Problem Frame

Every autofill bug fix risks breaking existing behavior because:

1. **12 question detectors** are scattered across `application_submit_common.py` with inconsistent patterns (some use keyword lists, some regex, some two-step gates). 5 of 12 have zero test coverage.
2. **Order-dependent detection chains** in each board's `_infer_step()` — reordering checks or adding a new one silently changes what matches. The city/state "live in" bug, salary comfort vs. expectation overlap, and education-falls-to-cover-letter bug all stem from this.
3. **Manual per-board wiring** — each new detector must be imported and inserted into the correct position in 9+ board scripts. The education fix touched 9 files in one commit.
4. **Keyword overlap between detectors** with no automated verification — "education background check" could match both education and background check detectors. Correctness depends on per-detector exclusion lists that must be manually maintained (O(n²) as detectors grow).
5. **Post-generation overrides are wasteful** — NDA/compensation questions generate an LLM answer, then replace it deterministically. Tokens and latency wasted.

## Requirements

- R1. **Single entry point**: Create `classify_question(label, field_type, application_profile, out_dir)` in `application_submit_common.py` that returns both the category (e.g., `"education"`, `"salary_comfort"`, `"cover_letter"`) and the deterministic answer value. Boards call this instead of maintaining their own detection chains.
- R2. **Priority-ordered dispatch**: Detectors run in an explicit, documented priority order. First match wins. More specific detectors (background check, sponsorship) run before broader ones (education, work authorization). Conflicts resolved by ordering, not per-detector exclusion lists.
- R3. **Regression test corpus**: Two layers: (a) hand-written edge cases for every known overlap scenario (education + background check, salary comfort + salary expectation, live-in yes/no + live-in free-text, sponsorship + work authorization) — these are mandatory regardless of harvest volume; (b) harvested real labels from past autofill reports/payloads in `output/` for breadth. Each label gets an expected classification. Any new detector that changes an existing label's classification fails the test. Tests also flag labels that match multiple detectors to verify the priority order handles them correctly.
- R4a. **Board migration — Tier 1 (payload-based boards)**: Lever, Ashby, Gem, Greenhouse, and Dover delegate to the unified classifier for all question-type detection. Board code only handles field-filling mechanics (building payload steps, option matching), not question classification.
- R4b. **Board migration — Tier 2 (browser-pipeline boards)**: Phenom, Workday, iCIMS, LinkedIn, and BambooHR call the classifier where possible, but detection may remain partially inline where it's embedded in DOM-traversal loops. Goal: classification logic uses the shared function even if the call site stays in the browser loop.
- R5. **Fallthrough to LLM**: When no detector matches, the field falls through to LLM generation. This is the current default behavior and must be preserved.
- R6. **Pre-generation filtering**: Questions that have deterministic answers are excluded from LLM generation before the LLM call, not overridden after. Eliminates wasted tokens on NDA, compensation, and other overridden categories.

## Success Criteria

- Adding a new question detector requires changes to exactly ONE file (`application_submit_common.py`) plus a test entry
- No existing label changes classification without an explicit, reviewed test update
- The real-label corpus catches regressions that keyword-level unit tests miss
- LLM generation is never called for questions with deterministic answers

## Scope Boundaries

- Not redesigning how boards fill fields (DOM interaction, API payloads) — only centralizing the classification + answer decision
- Not changing the LLM generation pipeline itself — only what gets sent to it
- Not adding new question detectors in this work — only consolidating existing ones
- Greenhouse's structured education fields (school/degree/discipline dropdowns) remain board-specific since they're not question classification — they're form structure

## Key Decisions

- **Unified classifier returns category + answer**: Maximum deduplication for question-type detection. Boards still handle non-question field routing (name, email, phone, LinkedIn, demographics) themselves — the classifier only replaces the 12 question-type detectors.
- **Explicit priority order over exclusion lists**: First-match-wins with ranked detectors. No per-detector deny-lists needed. Adding a new detector = deciding where it slots in priority.
- **Real labels over hand-written tests**: Ground-truth regression detection. Harvested from actual autofill runs rather than imagined edge cases.

## Dependencies / Assumptions

- Existing autofill report JSONs in `output/` contain enough label diversity to build a meaningful corpus
- All 12 existing detectors can be expressed as pure functions of (label, field_type, application_profile, out_dir) without needing board-specific context

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Technical] What is the return type of `classify_question()`? Probably a dataclass with `category: str`, `value: str | None`, `source: str`. Needs concrete design during planning.
- [Affects R2][Needs research] What is the correct priority order for all 12 existing detectors? Need to trace current behavior across boards to establish the canonical ordering.
- [Affects R3][Needs research] How many unique question labels exist in `output/`? Determines whether harvested labels add meaningful breadth beyond the mandatory hand-written overlap cases.
- [Affects R4b][Technical] Some boards (LinkedIn, BambooHR, Workday) do inline `from application_submit_common import` inside functions. How to handle those during migration?
- [Affects R4b][Technical] Boards that do browser-pipeline discovery (Phenom, Workday, iCIMS) have detection embedded in DOM-traversal loops. For each, determine whether the classifier can be called cleanly or if the call must stay inline in the loop.

## Next Steps

→ `/ce:plan` for structured implementation planning
