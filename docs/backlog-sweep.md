# Backlog Sweep Contract

Use this contract for any large Linear-Todo, stopped-job, or draft-review sweep where completion depends on exhausting a queue, not just landing code fixes.

## When This Is Required

- Any stopped-job or drafted-job sweep larger than 25 rows
- Any sweep that must exhaust every current Linear `Todo` issue before completion
- Any request with a hard denominator such as "review every draft in the snapshot"
- Any session where `complete` would otherwise depend on a manual queue pass

## Required Files

Every sweep needs immutable snapshots plus append-friendly results ledgers.

## Fast Start

Before any sweep session, resolve the active manifest:

```bash
uv run python scripts/resume_or_start_backlog_sweep.py --active
```

That command resumes the current active sweep when one is valid, or bootstraps a fresh active manifest when none is resumable.
Only replace an existing active sweep intentionally.

If Phase 1 has not been started for the active manifest yet, materialize the Linear Todo snapshot and ledger:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase1
```

At the start of Phase 2, materialize the stopped snapshot and ledger:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase2
```

At the start of Phase 3, materialize the draft snapshot and ledger:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase3
```

That staged flow creates:

- the Phase 1 snapshot
- the Phase 1 results ledger
- the Phase 2 snapshot
- the Phase 2 results ledger
- the Phase 3 snapshot
- the Phase 3 results ledger
- `.context/compound-engineering/todos/current_backlog_sweep.json`

Then the shortest verification command is:

```bash
uv run python scripts/verify_active_sweep.py --active
```

For the checked-in reusable prompt, use [`docs/runbooks/repeatable-backlog-sweep.md`](runbooks/repeatable-backlog-sweep.md).

If you explicitly want to replace the active sweep instead of resuming it, use:

```bash
uv run python scripts/init_backlog_sweep.py --new-run
```

### Snapshot TSV columns

Use this exact header for the Phase 1 Linear Todo snapshot:

```tsv
linear_issue_id	title	labels	status	related_job_id	related_output_dir	requires_user_input	captured_at_utc
```

Use this exact header for both Phase 2 and Phase 3 snapshots:

```tsv
id	company	role_title	board	output_dir
```

### Results ledgers

Each phase-start command writes a controller-derived results ledger header into `.context/compound-engineering/todos/`.
Do not hand-edit TSV rows. Append a row immediately after each snapshot item is handled, and prefer repo-native controller-backed commands or web actions.

For Phase 2 and Phase 3, the repo-native recorder remains:

```bash
uv run python scripts/record_backlog_sweep_result.py --active --phase phase2 --id <job-id> --outcome <outcome> --handled-via cli_manual
uv run python scripts/record_backlog_sweep_result.py --active --phase phase3 --id <job-id> --outcome reviewed_ready --handled-via draft_web_browser
```

For Phase 3 browser review, the preferred path is the local review surface action in `draft_web`, which records the same trace-backed row format.
Phase 1 issue handling must also go through the shared controller-backed workflow so the repo state and Linear mirror state stay synchronized.

For reference, the checked-in headers live here:

- [`docs/templates/phase1-linear-results-template.tsv`](templates/phase1-linear-results-template.tsv)
- [`docs/templates/phase2-stopped-results-template.tsv`](templates/phase2-stopped-results-template.tsv)
- [`docs/templates/phase3-draft-results-template.tsv`](templates/phase3-draft-results-template.tsv)

The checker is append-friendly. If a job is revisited, add another row; the latest row wins.
These columns are mandatory on the latest row for every snapshot item:

- `handled_via`
- `review_trace_path`
- `artifact_manifest_path`
- `proof_generated_at_utc`
- `repair_wave_fingerprint`
- `linear_sync_status`
- `linear_sync_payload_path`

For Phase 2 and Phase 3 rows, `evidence_paths` is also mandatory.

`evidence_paths` must be pipe-delimited local paths, for example:

```text
output/acme/submit/pre.png|output/acme/submit/post.png
```

Local evidence paths must exist on disk when the checker runs and must live under the job's `output_dir` or the per-job review directory created by the recorder.
Remote URLs do not satisfy the proof contract.

### Current-Wave And Linear-Sync Rules

For the latest row of every snapshot item:

- `linear_sync_status` must be `synced`
- repair outcomes must be recorded against the current `repair_wave_fingerprint`
- only explicit blocker/duplicate terminal outcomes are exempt from the current-wave requirement

In practice, that means later repo fixes force another attempt on previously unresolved repair rows.
The verifier does not accept stale repair attempts or rows that were never mirrored back to Linear.

## Shortcuts That Do Not Count

- bulk-populating Phase 2 or Phase 3 ledgers from audits, SQL, helper scripts, or existing artifacts without recording each row through the repo-native recorder
- using one shared batch evidence file to cover multiple snapshot rows
- treating snapshot creation, queue counts, rerun counts, or a failing verifier as completion of a phase
- marking Phase 3 `reviewed_ready` without a browser-backed review trace
- creating or updating a Linear issue as a substitute for attempting a repair on a likely repo-side defect

## Repair-First Decision Loop

Use this loop for every Linear Todo issue, stopped snapshot row, and drafted-row defect you encounter during the sweep.

1. Decide whether the problem is:
   - likely repo-side and repairable
   - clearly external/manual/user-blocked
   - ambiguous
2. If it is repo-side or ambiguous, you must attempt a concrete repair loop before parking or escalating:
   - reproduce the defect from current artifacts or a live rerun
   - identify the code path or runtime path that would need to change
   - implement the smallest generalized fix you can defend
   - run targeted verification and rerun/redraft the canonical job
3. Only park, classify as terminal, or leave a Todo issue open after one of these is true:
   - you have evidence the blocker is external or requires human input
   - you exhausted the allowed fix budget on that defect cluster
   - the row is genuinely unsupported and the missing capability is documented

Creating or updating a Linear issue does not satisfy this loop by itself.

## Allowed Outcomes

### Phase 1 (`linear_todo`)

- `fixed_verified`
- `blocked_user`
- `blocked_external`
- `duplicate_closed`
- `not_a_bug_closed`

### Phase 2 (`stopped`)

- `fixed_redrafted`
- `parked_requires_user_input`
- `nad_created`
- `duplicate_archived`
- `unsupported_parked`
- `terminal_external_confirmed`

### Phase 3 (`draft`)

- `reviewed_ready`
- `fixed_redrafted`
- `parked_requires_user_input`
- `nad_created`
- `duplicate_archived`

## Completion Gate

Use the fast coverage gate during the sweep:

```bash
uv run python scripts/check_backlog_sweep.py --active
```

Do not report `Final status: complete` unless this full verifier exits `0`:

```bash
uv run python scripts/verify_active_sweep.py --active
```

Natural-language summaries do not count. Green audits, rerun counts, and queue counts do not count.

## Prompt Delta

The existing prompt is close, but add these lines to make completion machine-checkable.

### Add Near The Top

```text
Run `uv run python scripts/resume_or_start_backlog_sweep.py --active` before Phase 1.
If the active manifest does not already define `phase1_snapshot` and `phase1_results`, run `uv run python scripts/init_backlog_sweep.py --start-phase phase1`.
At the start of Phase 2, run `uv run python scripts/init_backlog_sweep.py --start-phase phase2`.
At the start of Phase 3, run `uv run python scripts/init_backlog_sweep.py --start-phase phase3`.
```

### Add to Phase 2

```text
At the start of Phase 2, snapshot the exact set of jobs currently in status `stopped`.
That snapshot is the required review set and the denominator for Phase 2 completion.

Create a durable ledger at `.context/compound-engineering/todos/phase2-stopped-results-<date>.tsv`.
Append one row immediately after each snapshot job is handled.

Coverage-first rule:
- First pass must touch every snapshot job once before revisiting any job.
- Default to repair, not description. If a row reveals a likely repo-side defect, reproduce it, attempt a concrete generalized fix, and rerun the canonical job before you park it or mark it handled.
- Creating or updating a Linear issue does not count as that repair attempt.
- Only record a parked or terminal outcome after you have evidence that the blocker is genuinely external, requires human input, or remains unresolved after the allowed fix budget.
- Spend at most 15 minutes or 2 failed hypotheses on a single defect cluster before parking it and continuing.
- Audits and helper scripts may help triage, but they do not count as handling a row.
- Record each handled row through the repo-native recorder immediately after you inspect it.

Progress reporting during stopped-job review:
- After every 25 stopped jobs reviewed, stop and report:
  - reviewed so far: X / Y
  - jobs fixed/redrafted in that batch
  - NAD issues created/updated in that batch
  - jobs blocked on human input in that batch
- Then continue until Y is complete.
```

### Add Near The End

```text
You are explicitly authorized to use sub-agents and parallel agent work for independent batches in Phase 2 and Phase 3.
Use disjoint ownership and the shared repo-local ledgers so no snapshot item is skipped or duplicated.
Use the repo-native sweep recorder for every handled row; do not hand-backfill ledgers.

Shortcuts that do not count:
- bulk-populating Phase 2 or Phase 3 ledgers from audits, SQL, helper scripts, or existing artifacts
- using one shared batch evidence file for multiple snapshot rows
- treating a failing checker/verifier as permission to stop the sweep early
```

### Replace The Completion Rule With

```text
Final completion is forbidden unless:
1. the active manifest was resumed or started through the repo-native wrapper
2. Phase 1, Phase 2, and Phase 3 were started before work in that phase was counted
3. every row in the Phase 1 snapshot has a valid latest result row in the Phase 1 ledger
4. every row in the Phase 2 snapshot has a valid latest result row in the Phase 2 ledger
5. every row in the Phase 3 snapshot has a valid latest result row in the Phase 3 ledger
6. the latest row for every snapshot item is synced to Linear
7. the latest repair row for every non-exempt outcome matches the current repair wave
8. `uv run python scripts/check_backlog_sweep.py --active` exits 0
9. every Phase 3 `reviewed_ready` row was recorded from a browser review trace
```

## AGENTS Guidance

Keep AGENTS concise. Put the detailed sweep rules here, then add only a short pointer in `AGENTS.md`:

```text
Large stopped/draft sweeps must follow docs/backlog-sweep.md.
Do not claim completion without a passing `uv run python scripts/verify_active_sweep.py --active`.
```
