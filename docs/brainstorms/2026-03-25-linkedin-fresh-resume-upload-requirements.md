---
date: 2026-03-25
topic: linkedin-fresh-resume-upload-contract
---

# LinkedIn Easy Apply Fresh Resume Upload Contract

## Problem Frame

LinkedIn Easy Apply can carry forward a previously selected resume across different applications. That means the current role can already have a generated, company-specific resume in the repo while the live LinkedIn draft still points at an older attachment such as `Jerrison Li Resume - Linkedin.pdf`.

The user wants LinkedIn drafts to prefer the current role's company-specific resume and to attempt a fresh upload whenever LinkedIn exposes a replace or upload path. At the same time, the workflow should preserve throughput when LinkedIn hides resume controls entirely and still allows the draft to proceed to Review.

## Requirements

- R1. LinkedIn Easy Apply drafts shall use the current role's generated resume asset for the posting employer, not a generic LinkedIn-named resume.
- R2. For LinkedIn-sourced jobs, generated resume filenames shall use the actual employer posting the role, even when the application path is LinkedIn Easy Apply.
- R3. The flow shall reuse the current role's existing generated resume file if it already exists. It shall not require regenerating a brand-new resume file for every LinkedIn draft attempt.
- R4. Whenever LinkedIn exposes a visible upload, replace, change, or equivalent resume-selection path, the automation shall attempt a fresh upload of the current role's generated resume during that draft, even if LinkedIn already shows some resume selected.
- R5. If a visible upload or replace attempt results in live UI confirmation that the intended current-role resume is attached, the draft may continue normally.
- R6. If LinkedIn reaches Review without ever exposing a visible resume upload or replace path, the draft may continue to Review rather than failing closed solely because fresh-upload verification was unavailable.
- R7. If LinkedIn does expose an upload or replace path, the automation attempts it, and the result remains unclear or still shows the wrong attachment, the draft shall stop and remain incomplete.
- R8. LinkedIn draft artifacts shall distinguish between:
  - verified fresh upload
  - Review reached without visible resume controls
  - failed or unclear upload attempt
- R9. When LinkedIn exposes resume state, screenshot-visible live UI remains the source of truth for whether the intended resume attachment was actually achieved.

## Success Criteria

- A LinkedIn Easy Apply draft with visible resume upload or replace controls attempts a fresh upload of the current role's employer-named resume instead of trusting any preselected attachment.
- LinkedIn-origin resume files for a company such as `Asurion` are named for `Asurion`, not `LinkedIn`.
- If LinkedIn hides resume controls and still allows the flow to reach Review, the draft can proceed while artifacts clearly show that fresh-upload verification was unavailable.
- If LinkedIn exposes resume controls but the runtime cannot confirm the intended attachment after attempting the upload, the draft remains incomplete instead of silently continuing.

## Scope Boundaries

- NOT changing non-LinkedIn resume-upload behavior.
- NOT requiring resume regeneration on every LinkedIn draft attempt.
- NOT broadening this into a cross-board upload-replacement policy.
- NOT failing drafts solely because LinkedIn hid the resume controls and still allowed Review.
- NOT changing the separate global positive-fit screening policy.

## Key Decisions

- Fresh upload is required when LinkedIn exposes a visible upload or replace path.
- The existing current-role resume artifact is sufficient; no per-attempt regeneration is required.
- Hidden resume-control cases should optimize for throughput and may continue to Review.
- Visible-but-unverified upload attempts fail closed and keep the draft incomplete.
- Employer-name resume filenames are part of the LinkedIn correctness contract, not cosmetic polish.

## Dependencies / Assumptions

- The current pipeline can derive the posting employer and the current role's generated resume artifact.
- LinkedIn may preserve stale resume state across applications and retries.
- The report and screenshot artifacts can represent verified and unverified LinkedIn resume states distinctly.

## Alternatives Considered

- Regenerate a new resume file for every LinkedIn draft attempt: rejected because the user wants reuse of the current role's existing generated artifact.
- Only upload when the preselected attachment appears wrong: rejected because the user wants a fresh upload attempt whenever LinkedIn exposes that path.
- Fail closed whenever fresh-upload verification is unavailable: rejected because the user prefers Review throughput when LinkedIn hides resume controls entirely.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- Which LinkedIn UI markers should count as an exposed upload, replace, or change-resume path?
- What visible UI evidence should count as confirmation that the intended current-role resume is attached when LinkedIn truncates or reformats filenames?
- How should artifacts label the "Review reached without visible resume controls" state so it is explicit without reading as a runtime failure?

## Next Steps

-> `/prompts:ce-plan` to refresh the existing LinkedIn resume-upload plan so it matches this requirements contract
