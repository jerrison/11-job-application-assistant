---
date: 2026-03-28
topic: linked-resource-submit-answers
---

# Linked Resource Submit Answers

## Problem Frame

Some required application questions tell the candidate to inspect a directly linked public resource and answer from that source. Ramp's db-fiddle screening question is the clearest current example: the draft runtime generated an explicit fallback saying it could not access the external link, even though the repo already has the ability to fetch structured linked resources. That creates a trust and correctness gap. The system looks capable enough to answer, but submit-mode generated answers still treat the linked exercise as inaccessible.

The product gap is broader than Ramp or db-fiddle. Submit-mode generated answers currently disable external-resource access by default, so any supported board can produce low-quality or blocking answers when the question depends on a directly linked public page, PDF, CSV/JSON file, HTML table, or structured exercise. The fix should add a controlled, auditable, read-only linked-resource path for submit-time answer generation without turning draft mode into open-ended web research.

## Requirements

- R1. Submit-mode generated answers shall support a controlled linked-resource fetch path for directly linked, publicly accessible, read-only resources referenced in question labels or descriptions.
- R2. Supported linked-resource types shall include public HTML pages, PDFs, CSV/JSON files, HTML tables, and structured exercises such as db-fiddle pages when the linked content can be fetched during the draft attempt.
- R3. The linked-resource fetch scope shall be single-hop and direct-link only. The runtime shall not broaden into general web research, unrelated browsing, login-required content, or side-effecting interactions.
- R4. If a required question depends on a directly linked supported resource and the runtime cannot fetch or parse that resource, the draft shall fail closed and surface the exact resource failure instead of generating a fallback answer or treating the draft as ready.
- R5. If an optional question depends on a directly linked supported resource and the runtime cannot fetch or parse that resource, the draft may leave the answer blank, but it must log the exact resource failure in the current submit attempt artifacts and review surfaces.
- R6. When a linked resource is successfully fetched, the runtime shall save current-attempt evidence that includes the source URL, a fetched snapshot or extracted payload, and the structured facts used to derive the answer.
- R7. Generated answers that rely on linked-resource evidence shall remain grounded in that evidence and the existing candidate/source materials. They must not silently answer from unsupported assumptions when the linked resource was required for correctness.
- R8. The linked-resource path shall be shared across supported submit-time answer generation surfaces, not implemented as a board-local one-off. Greenhouse, Ashby, and other boards that use shared generated answers should inherit the same behavior.
- R9. The review artifacts for a draft with linked-resource-backed answers shall make it clear which answer used a linked resource and where the supporting evidence was saved.
- R10. Existing direct-answer behavior for questions that do not include linked resources shall remain unchanged.
- R11. The linked-resource path shall prefer deterministic extraction over model inference when the resource format supports it. For example, structured datasets or SQL exercises should be parsed into concrete derived facts before answer generation rather than asking the model to hallucinate from a raw link.
- R12. If the same linked resource appears across retries or reruns and the resource payload is unchanged, the runtime may reuse the saved current-attempt or matching cached extraction artifact rather than fetching it again, as long as the cache boundary stays explicit and auditable.

## Success Criteria

- A Ramp-like db-fiddle screening question no longer falls back to “I can’t access external links” when the linked resource is public and supported.
- Required linked-resource questions that cannot be fetched or parsed stop the draft with a clear, reviewable failure instead of producing a misleading fallback answer.
- Optional linked-resource questions that fail to fetch do not block the whole draft, but the exact failure is visible in current-attempt artifacts and review surfaces.
- A successful linked-resource-backed answer leaves behind auditable evidence showing the source URL and the derived inputs used to answer.
- The behavior works through the shared submit-time answer path rather than requiring one board-specific patch per board.

## Scope Boundaries

- In scope: directly linked, public, read-only resources referenced in submit-time application questions.
- In scope: deterministic extraction and evidence capture for structured linked exercises such as db-fiddle, CSV/JSON, HTML tables, and similar resources.
- Out of scope: arbitrary multi-hop browsing, open-ended web research, login-required resources, CAPTCHAs, authenticated dashboards, or interactive external tasks that require side effects.
- Out of scope: changing the meaning of the general draft rule or auto-submitting applications.
- Out of scope: broad prompt redesign for all generated answers; this is specifically about the linked-resource capability boundary.

## Key Decisions

- **Resource scope:** Broad but controlled direct-link, read-only public resources only.
- **Required failure behavior:** Required linked-resource questions fail closed when fetch or parse fails.
- **Optional failure behavior:** Optional linked-resource questions may remain blank, but the failure must be logged explicitly.
- **Evidence contract:** Save fetched-resource evidence and structured derived inputs, not just the final answer text.
- **Architecture direction:** Shared submit-time answer capability, not a Ramp-only or board-only exception.
- **Extraction posture:** Prefer deterministic extraction and derived facts for structured resources before answer generation.

## Dependencies / Assumptions

- The existing shared generated-answer path in `application_submit_common.py` remains the right orchestration point for most boards.
- The repo already has proof-of-concept pieces for db-fiddle fetching and analysis that can inform planning.
- Review surfaces can surface additional current-attempt artifacts without requiring a wholly new review product surface.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R1][Technical] Decide where the shared linked-resource fetch contract lives: inside `application_submit_common.py`, a new shared resource-fetch module, or a thin provider-agnostic orchestration seam plus format-specific helpers.
- [Affects R2][Technical] Decide the first supported resource adapters and their order of implementation: db-fiddle first, then generic HTML tables, CSV/JSON, and PDF, or another sequence.
- [Affects R6][Technical] Decide the artifact format and location for fetched-resource evidence and derived facts so current-attempt review surfaces can resolve them consistently.
- [Affects R11][Needs research] Confirm whether db-fiddle extraction should execute SQL directly inside the repo runtime, parse pre-rendered result tables from the linked page, or both.
- [Affects R12][Technical] Define the caching boundary for linked-resource evidence so retries can reuse safe artifacts without hiding stale or mismatched external data.

## Next Steps

-> `/prompts:ce-plan` for a structured implementation plan covering the shared linked-resource fetch contract, supported resource adapters, deterministic extraction flow, evidence artifacts, and review-surface integration
