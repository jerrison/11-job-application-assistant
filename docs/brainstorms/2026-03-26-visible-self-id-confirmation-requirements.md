---
date: 2026-03-26
topic: shared-draft-proof-contract-and-explicit-unavailability
---

# Shared Draft-Proof Contract, Stale Draft Semantics, and Explicit Unavailable Handling

## Problem Frame

Recent draft failures across multiple boards are symptoms of one product gap, not a pile of unrelated bugs. A draft can still appear successful when the live form shows missing or wrong profile-backed fields such as current location, phone number, LinkedIn URL, gender, race, or age range; when work-location choices ignore the user's San Francisco preference; when employer identity is derived from wrapper text such as `The Role` instead of the actual company; when previously answerable screening questions regress to blank; when a non-yes-no prompt that should be answerable from existing candidate truth causes the run to stall; when a linked screening exercise is left unanswered; when required screenshot proof is missing or repeated; when the final approve-and-submit surface is not captured; or when stale or legacy draft artifacts are still treated as the current draft. The same cluster also includes explicit unavailable pages that should become terminal archived outcomes instead of hanging or masquerading as incomplete drafts.

That creates a correctness, trust, and state-semantics problem:
- The system can plan or infer values without proving that the live UI, generated answers, and captured artifacts actually match what the draft claims.
- A draft reviewer can see a seemingly complete or current run even though required proof is missing, visible fields are blank, an answer stalled, the same screenshot was reused for multiple checkpoints, or the posting is dead.
- The earlier self-ID-only draft-blocker slice exposed the pattern, but the new regression cluster shows the contract must be broader than self-ID alone.

For this workflow, draft readiness must require shared proof-backed correctness across visible profile-backed fields, generated answers the product can support, truthful freeform answers that should be derivable from existing candidate inputs, and required artifacts, while explicit posting unavailability must become a separate terminal archived outcome and stale or legacy proof must stop presenting as current-ready.

## Requirements

- R1. Define a shared cross-board draft-proof contract for draft readiness, stale or legacy draft semantics, and explicit job-unavailability handling across CLI, TUI, web, worker-driven runs, and direct LLM-assisted runs.
- R2. Employer identity used for draft artifacts, filenames, review surfaces, and logging shall come from the actual posting employer, not generic wrapper headings, host names, or labels such as `The Role`.
- R3. If a supported board renders a visible profile-backed deterministic field and the candidate inputs contain a truthful configured value for that field, the draft shall not count as ready until that visible field is filled and confirmed from the live UI. This includes current location or current residence, phone number, LinkedIn URL, and visible self-ID or demographic fields such as gender, race or ethnicity, and age range when those values exist in the authoritative candidate inputs.
- R4. Current-residence and current-location prompts shall answer from the candidate's actual profile data, not from role location or posting location.
- R5. For work-location preference or availability prompts, if San Francisco is an offered valid choice, the draft shall prefer San Francisco as the selected work location. It shall not add other locations unless the prompt explicitly requires multiple selections or San Francisco is not offered.
- R6. Discrete positive-fit screening prompts shall continue to inherit the existing affirmative policy, and prompts the product can answer from existing source materials or shared answer policy shall not silently regress to blank or weaker responses.
- R7. Compliance-sensitive prompts remain on their truthful path. If work authorization, sponsorship, accommodation, age, self-ID, or other compliance-sensitive answers are known from the authoritative candidate inputs and required by the live form, the product shall fill or express those answers truthfully and confirm them instead of skipping them, leaving them stale, or marking the draft complete without proof.
- R8. Biographical and employment-history claims that are not covered by the existing affirmative policy, such as where the candidate has worked or whether they have startup experience, shall remain grounded in authoritative candidate materials rather than inferred from generic optimism or fabricated by the model.
- R9. If a required screening prompt is not a simple yes-no control but is answerable from existing candidate truth or shared answer policy, the draft shall generate the needed truthful prose response instead of stalling. This includes short explanatory answers such as authorization and sponsorship paragraphs when the underlying truth is already known.
- R10. If a required screening prompt includes a directly linked resource or exercise that the draft runtime can access during the attempt, the product shall use that provided resource to produce a grounded answer rather than leaving the question blank or pretending the draft is complete.
- R11. Draft readiness requires the required proof artifacts for that run, including the board-appropriate screenshot evidence the product claims to have captured and the final review or approve-and-submit surface when such a surface exists. Missing proof artifacts shall prevent the run from presenting as a successful draft.
- R12. Reused or repeated screenshots shall not satisfy multiple proof checkpoints unless the product can prove they represent the same required checkpoint. If the proof set repeats the same screenshot for distinct claimed checkpoints, the run shall treat that as missing or invalid proof.
- R13. When a visible configured field, generated answer, or required proof artifact is missing or unconfirmed on the first pass, the product shall retry within the same draft attempt before concluding failure.
- R14. If the retry still leaves a required field, generated answer, or proof artifact missing or unconfirmed, the run shall fail closed as incomplete and surface the exact missing or regressed item instead of presenting the draft as ready.
- R15. If the live application explicitly indicates that the role or application is unavailable, closed, invalid, or no longer accepting applications, the run shall stop in a durable not-available outcome, automatically archive the job, and record evidence and logging explaining why.
- R16. The exact missing, wrong, regressed, stale, legacy, or unavailable reason must be visible across the existing review surfaces and artifacts rather than being discoverable only inside board-specific logs.
- R17. Draft-status semantics shall distinguish current active draft attempts, stale or legacy draft evidence, blocked incomplete drafts, and explicit unavailable archived outcomes. A stale or legacy draft shall not present as current-ready or ambiguously remain in draft status without explanation.
- R18. Legacy artifacts from earlier attempts or older proof expectations shall not satisfy the current draft-proof contract unless they are explicitly matched to the active attempt and required checkpoints.
- R19. This contract shall reuse existing truthful and compliance-sensitive policies rather than overriding them. Work authorization, sponsorship, compensation, and other compliance-sensitive answers stay on their existing truthful paths; this brainstorm broadens proof and readiness enforcement, not answer fabrication.
- R20. The contract shall apply globally across supported boards and runtime surfaces, even if planning sequences implementation in slices.

## Success Criteria

- A draft like the Linktree examples does not count as ready if visible current location, phone number, LinkedIn URL, gender, race, or age range remain blank or wrong on the live form.
- If a work-location prompt offers San Francisco, the draft selects San Francisco only unless the prompt explicitly requires multiple locations.
- If a positive-fit screening prompt, linked screening exercise, or previously answerable generated screening question regresses to blank, the draft stays incomplete with the exact prompt surfaced rather than silently succeeding.
- If a work-authorization, sponsorship, accommodation, or similar truthful prompt requires short prose rather than a yes-no click, the draft provides the truthful answer instead of stalling on the control type.
- If a question asks about prior employers or startup experience, the answer matches the authoritative candidate materials rather than generic optimism or hallucinated history.
- If the page explicitly says the job is unavailable or closed, the run lands in a not-available archived outcome with evidence and logging.
- A draft cannot appear successful when its required screenshot proof is missing, repeated, or missing the final review or approve-and-submit surface.
- A stale or legacy draft cannot still appear as the current in-draft or draft-ready state without an explicit stale or legacy reason.
- The same readiness and unavailable rules hold across boards and surfaces instead of remaining a board-specific patch set.

## Scope Boundaries

- Not changing the `--draft` rule or auto-submitting applications.
- Not broadening the affirmative policy to compensation, work authorization, sponsorship, or self-ID truthfulness questions that must stay on their existing truthful paths.
- Not inventing factual biography such as past employers or startup experience beyond what the authoritative candidate materials support.
- Not requiring every open-ended question to be answered automatically; the requirement is that questions the product can already answer from current source materials or shared policy must not silently regress to blank.
- Not redefining the review UI from scratch when the existing incomplete or unconfirmed artifact flow can carry the state.
- Not treating transient service interruptions or auth issues as permanently unavailable postings unless the live application explicitly shows the role or application is unavailable, closed, or invalid.
- Not requiring multi-location selections unless the prompt itself does.
- Not treating inaccessible linked exercises or off-flow external tasks as silently successful; if the runtime cannot access or solve a required linked task within the supported draft flow, it must surface that as incomplete rather than fabricate success.

## Key Decisions

- The right framing is a shared draft-proof contract, not a pile of board-local bug fixes.
- Visible live-form confirmation and required artifacts are the source of truth for draft readiness.
- The earlier self-ID-only blocker rule is now widened to all visible profile-backed deterministic fields and required draft proof artifacts.
- San Francisco-first is the default work-location preference when the prompt offers it as a valid option.
- Compliance-sensitive answers remain truthful; the bug is missing, stale, or unconfirmed truthful answers, not insufficiently aggressive answer policy.
- The existing affirmative policy stays limited to its current positive-fit domain, while factual biography and employment-history claims remain source-backed.
- Repeated screenshots and missing final review or approve-and-submit proof are draft-integrity failures, not cosmetic issues.
- Stale or legacy draft state belongs in the same contract because proof quality and status semantics are coupled.
- Linked screening exercises are in scope when the prompt itself provides the resource and the runtime can access it during the draft attempt.
- Explicit posting unavailability is a separate terminal rule: auto-archive with evidence, not draft-incomplete.
- Previously answerable screening prompts regressing to blank are trust failures, not acceptable best-effort misses.

## Dependencies / Assumptions

- Authoritative candidate inputs already contain truthful values for profile-backed deterministic fields that the product is expected to prove on the live form when shown.
- Existing draft artifacts, incomplete states, and review surfaces can expose exact missing or regressed items without inventing a brand new product workflow.
- Existing affirmative-policy and answer-regeneration work can be reused rather than redefined.
- The runtime can already access at least some linked question resources and short prose answer paths during a draft attempt, so the gap is capability integration and proofing, not a new product mode.

## Alternatives Considered

- Fix each reported issue as an isolated bug: rejected because the failures reveal the same product-contract gap and would likely recur on other boards or surfaces.
- Keep self-ID as the only visible draft blocker: rejected because the same correctness failure now appears in current location, phone, LinkedIn, age range, and other profile-backed fields.
- Auto-archive any broken draft: rejected because unresolved fields and missing proof are draft-integrity failures, not proof that the posting itself is unavailable.
- Keep generated-answer regressions out of scope: rejected because blank or downgraded answers on questions the product previously handled directly undermine draft trust.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- What shared registry or categorization should define visible profile-backed deterministic fields, truthful freeform candidate-truth prompts, and source-backed biography claims so boards inherit the same contract without drift?
- Where should San Francisco-first work-location preference override earlier role-location heuristics without breaking prompts that truly require role-aligned or multi-select answers?
- What is the cleanest retry boundary so the system retries real fill, answer-generation, linked-task solving, and screenshot-capture work once without masking persistent failures?
- What durable status and evidence markers should represent stale or legacy draft state across queue views, job detail, logs, and review surfaces without confusing it with active incomplete drafts?
- What heuristic is sufficient to detect repeated screenshots or missing final review or approve-and-submit evidence without overfitting to board-specific layouts?
- What boundary should planning use for "linked screening task the runtime is expected to solve now" versus "external research task that should surface as unresolved"?

## Next Steps

-> `/prompts:ce-plan` for a structured implementation plan covering shared draft-proof enforcement, San Francisco-first work-location preference, explicit unavailable handling, and generated-answer regression protection across supported boards and surfaces
