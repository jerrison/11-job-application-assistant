# Draft Answer Verifier Design

Date: 2026-04-01
Status: Approved for spec review
Related:

- [2026-03-15-application-draft-mode-design.md](./2026-03-15-application-draft-mode-design.md)
- [2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md](./2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md)
- [2026-03-31-self-repair-supervisor-and-rendered-state-audit-design.md](./2026-03-31-self-repair-supervisor-and-rendered-state-audit-design.md)
- [../../board-architecture.md](../../board-architecture.md)
- [../../output-structure.md](../../output-structure.md)

## Problem

The repo already has strong draft safety for rendered form state:

- `--draft` always stops before submit
- screenshots are the source of truth
- deterministic option fields are moving toward exact rendered-state verification
- `answer_refresh_status.json` proves whether fresh generated answers were rewritten for the current request
- `pending_user_input.json` already fails closed when the runtime knows it does not have enough truthful information

That still leaves one important gap:

- a generated free-text answer can be present in `submit/application_answers.json`
- the live field can render exactly that answer
- the answer can still be weak, partially unsupported, or subtly untruthful

Today, that class of error is mostly caught by human draft review after generation. The user asked whether a subagent reviewing a first worker's draft would help. The answer is yes, but only if the second model acts as a verifier with explicit grounding rules. A second model that simply "likes" the answer is not a correctness guarantee.

## Relationship To Existing Draft Safety

This design extends the current contracts rather than replacing them.

Existing mechanisms remain responsible for:

- deterministic question classification and shared policy answers
- exact-match rendered-state checks for deterministic option fields
- current-attempt proof selection and stale-artifact cleanup
- answer refresh proof state
- human draft review and edit-to-fix loops
- `pending_user_input.json` for truthful fail-closed cases

This design adds a new layer between answer generation and draft-ready status:

1. classify each answer into a verification lane
2. run a reference-guided verifier only for the lanes that need it
3. fail closed when the verifier finds unsupported or policy-breaking claims
4. surface verifier proof in the same draft-review surfaces that already expose answer-refresh and screenshot proof

## Goals

- Catch unsupported or fabricated claims in generated application answers before a draft is treated as ready.
- Keep deterministic answers on deterministic paths instead of paying reviewer latency for questions that already have a source of truth.
- Use repo-local source material as the verifier's ground truth:
  - `application_profile.md`
  - `master_resume.md`
  - `work_stories.md`
  - `candidate_context.md`
  - parsed JD and linked-resource evidence already fetched by repo code
- Preserve current fail-closed behavior: if the system cannot support an answer truthfully, write review artifacts and stop.
- Keep the verifier surface-agnostic across CLI, TUI, web, worker, and direct LLM-driven runs.
- Produce durable proof artifacts so later repair loops can cluster wrong-answer failures instead of treating them as one-off anecdotes.

## Non-Goals

- Replacing human draft review.
- Letting the reviewer invent better answers from general world knowledge.
- Using reviewer approval as a substitute for rendered-state proof.
- Adding live web browsing during submit-time answer verification.
- Rewriting `submit/application_answers.json` into a large new schema that would break existing readers.

## Approaches Considered

### 1. Keep Human Review As The Only Check

Pros:

- simplest implementation
- no extra provider cost

Cons:

- catches bad answers only after generation
- difficult to measure regressions before users notice them
- does not create machine-readable proof for repair clustering

Rejected.

### 2. Have A Second Worker Rewrite The First Worker's Answer

Pros:

- may improve phrasing quality

Cons:

- two generators can share the same misconception
- little separation between "author" and "judge"
- encourages silent rewriting instead of explicit blocking
- hard to tell whether the final answer is actually grounded

Rejected as the default pattern.

### 3. Generator + Independent Reference-Guided Verifier

Recommended.

Pros:

- preserves one clear author and one clear judge
- keeps the reviewer focused on truthfulness and policy, not style preferences
- fits current repo contracts: draft proof, pending-user-input, answer refresh, and audit loops
- creates explicit artifacts for later debugging and repair

Cons:

- adds another provider call for some questions
- requires a careful rubric so the verifier does not become noisy

## Selected Design

### A. Three Verification Lanes

Every planned answer should enter one of three lanes.

#### Lane 1: Deterministic Rendered-Only

Use this when the answer already comes from a canonical deterministic source or shared question policy.

Examples:

- work authorization
- sponsorship
- EEO / self-ID defaults
- compensation deflection
- product-usage yes/no
- deterministic anti-bot identity checks
- option selections and profile-backed single-line text

Behavior:

- no verifier model call
- answer must still pass current rendered-state and proof checks
- if the deterministic source is missing or ambiguous, fail closed to `pending_user_input.json`

#### Lane 2: Reference-Verified Generated Text

Use this when the answer is free text but should still be grounded in repo-controlled source material.

Examples:

- "Why this company?"
- "Why this role?"
- "Describe your startup experience."
- short domain-fit answers derived from resume bullets, work stories, candidate context, or linked resources

Behavior:

- generation runs first
- verifier runs after normalization, overrides, and linked-resource enrichment
- verifier may:
  - approve
  - request one guided regeneration
  - block to `pending_user_input.json`

#### Lane 3: User-Required

Use this when the repo does not have enough truthful information to answer safely.

Examples:

- questions asking for unavailable portfolio details
- prompts requiring novel personal preference not represented in source material
- long bespoke prompts whose requested evidence is missing from repo inputs
- prompts that explicitly require a credential, license, or experience level the repo cannot support

Behavior:

- do not generate
- write `pending_user_input.json`
- mark the draft as blocked until the user edits or overrides the answer

### B. Reviewer Role: Judge, Not Co-Author

A reviewer subagent is useful only in a narrow role:

- read-only
- no code edits
- no browsing
- no filesystem mutation outside writing its own verifier artifact
- no authority to silently replace answers

The reviewer should judge a candidate answer against explicit source material and return a structured verdict. It should not be asked to "improve" the answer on the same pass.

Recommended orchestration:

1. generator produces the draft answer
2. verifier judges that answer against the source bundle
3. if the issue is repairable by better grounding or coverage, the generator gets one concise retry brief
4. verifier judges the retry
5. if still blocked, fail closed

Do not allow indefinite generator <-> reviewer loops. One verifier-guided regeneration per question per submit attempt is enough.

### C. Shared Verifier Input Contract

Add a shared module, tentatively `scripts/answer_verifier.py`, that receives a normalized bundle built from existing artifacts.

Minimum inputs:

- `meta`
  - company
  - role title
  - board
  - submit dir name
- `question`
  - `field_name`
  - `label`
  - `required`
  - `type`
- `answer`
  - normalized planned answer from `submit/application_answers.json`
  - answer provider and generation timestamp if available
- `verification_lane`
  - `deterministic_rendered_only`
  - `reference_verified_generated_text`
  - `user_required`
- `source_bundle`
  - normalized excerpts from `application_profile.md`
  - relevant resume bullets from `master_resume.md`
  - relevant narrative details from `work_stories.md`
  - relevant fit/context snippets from `candidate_context.md`
  - parsed JD summary and company/role language
  - linked-resource evidence already fetched by repo code
- `constraints`
  - no fabricated metrics
  - no unsupported credential claims
  - no numeric compensation answers
  - draft-mode fail-closed policy

The verifier should see excerpts, not entire raw files, when possible. That keeps the prompt smaller and makes its grounding responsibility clearer.

### D. Verifier Rubric

The verifier should return a structured result per question using the following rubric.

#### Hard-Gate Criteria

These must pass for the answer to be accepted.

1. `answers_the_prompt`
   - The answer actually addresses the asked question.
   - It is not generic filler or a partial tangent.

2. `grounded_in_allowed_sources`
   - Every substantive claim is supported by the provided source bundle or deterministic linked-resource evidence.
   - If support is missing, the answer is blocked.

3. `truthful_and_non_fabricated`
   - No invented companies, products, metrics, credentials, or timelines.
   - No implication of experience that source material does not support.

4. `policy_compliant`
   - No numeric salary answer.
   - No unsupported degree/license/certification claim.
   - No contradiction of work-authorization or other profile-backed truths.

#### Soft Criteria

These should be reported but do not block on their own unless combined with a hard-gate failure.

5. `tone_and_length_fit`
   - concise enough for the field
   - professional but not generic
   - uses company/role language when supported by source material

6. `specificity`
   - includes enough concrete detail to be credible
   - does not drift into unnecessary verbosity

#### Escalation Criterion

7. `requires_user_input`
   - The verifier may explicitly say the answer cannot be supported from available repo sources and should be escalated to user input instead of regenerated.

### E. Verifier Outcomes

Per question, the verifier returns one of:

- `approved`
- `retry_with_feedback`
- `blocked_requires_user_input`
- `blocked_system_failure`
- `not_applicable`

Rules:

- `approved`: question passes all hard gates
- `retry_with_feedback`: question is answerable from available sources, but the current answer missed required grounding or coverage
- `blocked_requires_user_input`: truthful answer is not possible from repo sources
- `blocked_system_failure`: verifier timed out, crashed, or returned an unusable result
- `not_applicable`: deterministic or empty lane where no verifier call should have run

### F. Artifact Schema

Keep the existing `submit/application_answers.json` contract stable. Add verifier sidecars instead of widening the primary answer payload.

#### Output Root

Add:

- `answer_verification_status.json`

Suggested shape:

```json
{
  "state": "unknown",
  "message": "Legacy draft that predates answer verification.",
  "request_id": null,
  "submit_dir": "submit",
  "answer_provider": null,
  "verifier_provider": null,
  "generated_answer_count": 0,
  "verified_answer_count": 0,
  "blocked_answer_count": 0,
  "generated_at_utc": null
}
```

States:

- `unknown`
- `pending`
- `verified`
- `not_applicable`
- `blocked`
- `failed`

`answer_verification_status.json` should live at the role output root for the same reason `answer_refresh_status.json` does: every runtime surface can read a single shared state without a database lookup.

#### Active Submit Directory

Add:

- `submit/answer_verification.json`
- optionally `submit/answer_verification_raw.txt` for provider-specific debugging

Suggested shape:

```json
{
  "generated_at_utc": "2026-04-01T20:15:00+00:00",
  "request_id": "verify_123",
  "board": "greenhouse",
  "submit_dir": "submit",
  "answer_provider": "openai",
  "verifier_provider": "claude",
  "status": "blocked",
  "summary": {
    "question_count": 3,
    "approved_count": 1,
    "retry_count": 1,
    "blocked_count": 1
  },
  "questions": [
    {
      "field_name": "why_company",
      "label": "Why this company?",
      "required": true,
      "verification_lane": "reference_verified_generated_text",
      "answer_text": "I am excited about your AI platform because...",
      "verdict": "retry_with_feedback",
      "confidence": "medium",
      "score": 0.72,
      "rubric": {
        "answers_the_prompt": {"pass": true, "notes": ""},
        "grounded_in_allowed_sources": {
          "pass": false,
          "notes": "Mentions platform work not supported by source bundle."
        },
        "truthful_and_non_fabricated": {
          "pass": false,
          "notes": "Claims direct platform experience not present in resume or work stories."
        },
        "policy_compliant": {"pass": true, "notes": ""},
        "tone_and_length_fit": {"pass": true, "notes": ""}
      },
      "suggested_action": "retry_with_feedback",
      "feedback_for_regeneration": [
        "Remove unsupported claim about direct platform ownership.",
        "Ground motivation in workflow automation, AI product work, and company language from the JD."
      ],
      "source_refs": [
        "master_resume.md",
        "work_stories.md",
        "candidate_context.md",
        "content/jd_parsed.json"
      ]
    }
  ]
}
```

Notes:

- `score` is optional and should be used for monitoring, not as the sole gate.
- `verdict` drives behavior; hard blockers override a high soft score.
- `source_refs` should list the source artifacts the verifier actually used.

### G. Orchestrator Behavior

The verifier should run after generated answers are finalized for the current attempt:

1. generate answers
2. normalize multi-selects
3. apply deterministic overrides and draft overrides
4. write or stage `submit/application_answers.json`
5. run verifier for lane-2 questions
6. persist verification sidecars
7. continue only if verification state is `verified` or `not_applicable`

Behavior by outcome:

- `verified`
  - proceed normally
- `not_applicable`
  - proceed normally
- `retry_with_feedback`
  - allow one generator retry
  - rerun verifier on the retry
- `blocked_requires_user_input`
  - write `pending_user_input.json`
  - mark draft as blocked
  - do not let the job silently land as ready
- `blocked_system_failure`
  - mark verifier state `failed`
  - treat as repairable system failure, not user blame

The verifier must never downgrade an existing truthful terminal result. If the current attempt already ended as `pending_user_input`, `unsupported`, `auth_*`, `already_applied`, or another terminal current-attempt result, that result remains the source of truth.

### H. Draft Review Surface Integration

`draft_summary.md`, the web draft UI, and TUI review surfaces should gain an `Answer Verification` section that mirrors the shared root status plus per-question blockers.

Display rules:

- show the shared verifier state near the existing answer-refresh proof
- if any question is blocked, list it above the generic application-answer dump
- show verifier notes and cited source refs for blocked or retryable answers
- keep screenshot proof and rendered-state blockers ahead of style-only verifier comments

Suggested priority order inside draft review:

1. missing proof / pending user input / terminal blockers
2. answer verification blockers
3. answer-refresh proof
4. rendered screenshot and report artifacts
5. full answer listing

### I. Failure Fingerprints And Repair Integration

Verifier failures should emit normalized fingerprints so the existing repair-loop and self-repair-supervisor work can cluster them.

Suggested fingerprint families:

- `shared:answer_verifier:unsupported_claim`
- `shared:answer_verifier:policy_violation`
- `shared:answer_verifier:missing_source_support`
- `shared:answer_verifier:verifier_timeout`
- `shared:answer_verifier:verifier_parse_failure`

That makes it possible to separate:

- content problems that require better prompting or classifier routing
- system failures that require code fixes
- truthful user-input gaps that should remain manual

### J. Testing Strategy

The verifier needs both deterministic unit tests and a curated eval set.

#### Unit Tests

Add targeted tests for:

- lane classification
- verifier state transitions
- single retry limit
- `pending_user_input` escalation when support is missing
- draft summary rendering of verifier blockers
- root-state resolution from active submit dir

#### Eval Set

Build a small repo-local eval set from:

- prior `manual_review.json` cases
- wrong-answer fixes caught through draft edits
- compensation / credential / sponsorship regression cases
- linked-resource answers
- company-fit and startup-experience prompts

Each eval case should include:

- the prompt
- source bundle
- candidate answer
- expected verifier verdict

This gives the repo a regression suite for prompt or provider changes.

## Tentative File Impact

Likely touched files for implementation:

- `scripts/application_submit_common.py`
- `scripts/answer_verifier.py` (new)
- `scripts/draft_manager.py`
- `scripts/build_draft_summary.py`
- `scripts/job_web.py`
- `scripts/static/app.js`
- `tests/test_submit_application.py`
- `tests/test_draft_manager.py`
- `tests/test_answer_verifier.py` (new)

## Recommended Default

Use a reviewer subagent or secondary model only for lane-2 generated free-text answers. Keep it read-only, reference-guided, and fail-closed. Deterministic answers should continue to rely on existing source-of-truth paths plus rendered-state proof. User-required questions should skip generation entirely and go straight to `pending_user_input.json`.
