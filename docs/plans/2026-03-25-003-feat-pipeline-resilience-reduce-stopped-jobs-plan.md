---
title: "feat: Pipeline resilience — reduce stopped jobs from 54% to <25%"
type: feat
status: active
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md
---

# Pipeline Resilience: Reduce Stopped Jobs

## Enhancement Summary

**Deepened on:** 2026-03-25
**Refreshed on:** 2026-03-27
**Research agents used:** Python Reviewer, Performance Oracle, Architecture Strategist, Code Simplicity Reviewer, Pattern Recognition Specialist, Data Integrity Guardian, Best Practices Researcher

### Key Improvements from Deepening
1. **Critical: stale `retry_after` can permanently hide jobs** — added safety-net cap + requeue-path audit
2. **Critical: missing CAS guard** in `_auto_retry_if_transient` raw SQL — added status check
3. **Retry storm mitigation** — reduced `_RATE_LIMIT_RETRIES` from 5→2, documented total retry budget
4. **Simplified R6** — dropped escalation complexity, use fixed cooldown on WorkerPool
5. **Simplified R2** — fixed delay table `[120, 480, 1800]` replaces exponential formula
6. **Rearchitected R4** — use provider-native structured output instead of prompt mutation
7. **Fixed R5 counter-mixing** — derive timeout escalation from last failure_type, not shared fix_attempts
8. **Three-tier error classification** (permanent > transient > unknown) replaces whitelist-only
9. **Sentinel date for retry_after** — fixes composite index performance (NULL breaks range scan)
10. **Collapsed to 3 phases** from 5 (phases 3–5 were always parallel)
11. **Refresh note** — origin scope now explicitly prioritizes reducing future stopped jobs system-wide first; current stopped-job inspection is supporting evidence, not a competing primary objective

## Overview

54% of jobs (219/403) end in `stopped` status. Excluding intentional stops (user_stopped, duplicate, unsupported = 29), **190 jobs stopped due to preventable failures**. The two dominant root causes are LLM failures (35%, 67 jobs) and rate limiting (29%, 55 jobs). The refreshed origin requirements now make future stopped-job reduction the primary target, with current stopped-job investigation used only to validate priorities and collect evidence. A 2026-03-27 repo scan shows much of the retry/backoff/cooldown foundation is already present, so this plan now focuses on preserving those contracts, finishing the remaining parity gaps, and keeping verification aligned with the intended system-wide outcome.

## Problem Statement / Motivation

The pipeline still needs a clean resilience contract at three layers:

1. **Provider fallback must stay real, not just configurable** — the codebase and local workspace now expose chain mode, but the chain can still be bypassed by stale provider state, worker env inheritance, or unverified fallback providers. The plan must preserve end-to-end provider parity rather than assuming the config alone closes R1.
2. **Retry/backoff behavior is cross-cutting and easy to regress** — the current repo already carries `retry_after`, board cooldown, and non-transient classification behavior across several files. That makes the remaining work more about contract preservation, reset/requeue parity, and operational confidence than about greenfield implementation.
3. **JSON reliability is uneven across generation surfaces** — the shared submit-answer path now has strict-schema support plus retry/repair handling, but the broader asset-generation surfaces named in the original plan do not yet obviously share that same contract. The plan needs to decide where R4 should apply now, not assume one broad rollout without evidence.

(see origin: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`)

## Requirements Trace

- **R1:** Keep provider fallback configured and verifiable end-to-end for `openai → gemini → claude`.
- **R2:** Ensure queued job pickup respects backoff via durable `retry_after`.
- **R3:** Prevent permanent failures from burning transient retry budget.
- **R4:** Use strict JSON contracts or bounded retry/repair at the JSON-producing seams that materially contribute to stopped jobs.
- **R5:** Escalate asset timeouts only when the prior failure was a timeout.
- **R6:** Pause board-level pickup after rate-limit detection to prevent stampedes.

## Proposed Solution

Six requirements (R1–R6) addressing the three layers, implemented in 3 phases:

| Phase | Requirements | What Changes | Impact |
|-------|-------------|--------------|--------|
| 1 | R3 | Three-tier error classification | 4+ wasted retry cycles eliminated |
| 2 | R2 + R6 | `retry_after` column + board cooldown | 55 rate-limit stops reduced |
| 3 | R1 + R4 + R5 | Provider chain + structured output + timeout escalation | 88 failures reduced |

## Refresh Notes (2026-03-27)

- **Primary target remains future batches:** current stopped-job review should confirm failure mix and provide tracker-ready evidence, but it should not turn this plan into a one-off recovery playbook.
- **R2, R3, R5, and R6 appear implemented in the current repo:** `scripts/pipeline_orchestrator.py`, `scripts/job_db.py`, `scripts/job_worker.py`, and targeted tests already encode the core retry/backoff/cooldown behavior this plan originally proposed.
- **Workspace config already points at the intended chain:** the local `.env.local` currently uses `openai,gemini,claude` and carries a restart note for worker env inheritance. R1 therefore remains a verification and parity concern, not a fresh architecture decision.
- **R4 is the main remaining scope-shaping question:** structured JSON is clearly present for the shared submit-answer path, but not yet obviously generalized across the other JSON-emitting asset-generation seams named in the original plan. The remaining implementation should narrow to the seams that still create stopped jobs rather than broad speculative rollout.

## Technical Considerations

### Design Decisions

**D1. `retry_after` sentinel date** — Use `'1970-01-01 00:00:00'` instead of NULL for "no delay." This enables a single range scan on the composite index `(status, retry_after)`. The `IS NULL OR` pattern breaks SQLite's index usage — with 40 workers polling every 2s, this matters. Filter becomes simply `retry_after <= datetime('now')`.

**D2. R2/R6 interaction (backoff + cooldown stacking)** — Board cooldown and per-job `retry_after` are independent checks. Effective delay = `max(retry_after, board_cooldown_until)`. Board cooldown stored in-memory on the `WorkerPool` instance (matching the existing `_active_boards` pattern). Cooldown resets on restart, which is acceptable since rate limits are transient.

**D3. Three-tier error classification (R3)** — Replace the current whitelist-only transient check with a three-tier system:
1. Check `failure_type` column first — if in `_NON_TRANSIENT_TYPES`, stop immediately (PERMANENT)
2. Check `_TRANSIENT_PATTERNS` — if matched, retry aggressively (TRANSIENT)
3. Default: retry cautiously with longer backoff and fewer attempts (UNKNOWN)

This catches novel transient errors that the current whitelist misses, while still blacklisting known-permanent failures. (Per AWS, Temporal, and Spring resilience best practices.)

**D4. R5 timeout escalation source** — Derive from last `failure_type`, not from shared `fix_attempts` counter. `fix_attempts` is incremented by both rate-limit retries and auto-fix attempts — a rate-limited job that then times out would get an inappropriately escalated timeout. Instead: if previous `failure_type == 'timeout'`, escalate; otherwise use default timeout.

**D5. R4 rearchitected: structured output where the Python caller owns JSON parsing** — The current repo already applies this pattern on the shared submit-answer path in `scripts/application_submit_common.py`: OpenAI gets a JSON schema, malformed output gets one bounded retry, and `json_lenient.py` remains the repair fallback. Continue using provider-native structured output only at Python-owned JSON seams that still contribute to stopped jobs. Do not mutate shell-owned prompt files just to force stricter JSON.

**D6. R6 cooldown: fixed duration, on WorkerPool** — Simple `_board_cooldowns: dict[str, datetime]` with `threading.Lock` on the `WorkerPool` instance (alongside `_active_boards`). Fixed duration via `BOARD_COOLDOWN_SECONDS` env var (default 300). No escalation, no dataclass, no window tracking. Fires only on rate-limit patterns, not all transient errors.

**D7. Retry budget cap** — Total retry budget across all layers is `(RATE_LIMIT_RETRIES + 1) * len(providers) * (MAX_AUTO_RETRIES + 1)`. With current defaults (5 inner × 3 providers × 4 outer = 60 calls), this is a retry storm. Reduce `_RATE_LIMIT_RETRIES` from 5 to 2 to cap at `3 × 3 × 4 = 36` calls. Further reduction comes from board cooldown preventing the outer loop from firing repeatedly.

### Cross-Cutting Gotchas (from learnings + deepening)

1. **All requeue paths must set `provider = NULL`** — otherwise jobs are locked to the original provider and bypass the fallback chain. (from `docs/solutions/integration-issues/adding-new-llm-provider.md`)

2. **All requeue paths must clear `retry_after`** — Every code path that resets a job to `queued` must also set `retry_after = '1970-01-01 00:00:00'` (sentinel). Affected paths: `_auto_retry_if_transient`, `reset_stale_jobs`, kill-worker requeue, TUI stop-workers, web stop-workers, `regenerate_job`. A stuck `retry_after` in the far future permanently hides a job from `get_pending_jobs`.

3. **Safety-net cap in `reset_stale_jobs`** — Add: `UPDATE jobs SET retry_after = '1970-01-01' WHERE status = 'queued' AND retry_after > datetime('now', '+1 hour')`. Prevents permanent job loss from buggy backoff values.

4. **CAS guard in `_auto_retry_if_transient`** — The current raw SQL uses `WHERE id = ?` without status check. Add `AND status IN ('stopped', 'queued', ...)` to prevent overwriting concurrent status changes from kill-worker or stale-reset.

5. **Timezone consistency** — Use `datetime('now', '+N seconds')` in SQLite for computing `retry_after` values, not Python datetime objects, to guarantee UTC consistency with the `datetime('now')` comparison in `get_pending_jobs`.

6. **Worker env inheritance** — Workers inherit a frozen env snapshot from the web server. Changing `.env.local` requires restarting the web server.

7. **Shell validation** — `llm_common.sh` has a hardcoded provider case statement that must accept `openai`, `gemini`, and `claude`.

### Architecture / Performance

- `retry_after` sentinel + composite index `(status, retry_after)`: single range scan per poll, negligible overhead
- Board cooldown: in-memory dict on WorkerPool, zero DB overhead
- 40 threads polling every 2s: ~20 reads/s — well within SQLite WAL capacity
- Reduce `_RATE_LIMIT_RETRIES` 5→2: prevents 2.5-minute worker stalls from in-place rate-limit sleep
- Worst-case single job: 1800s × 3 providers = 5400s (90 min). Acceptable with 40 workers.
- Consider increasing `busy_timeout` from 30s to 60s for 40-thread workloads (per SkyPilot research)

## System-Wide Impact

- **Worker polling loop** (`job_worker.py:236`): Modified to filter by `retry_after` and board cooldown
- **WorkerPool** (`job_worker.py`): New `_board_cooldowns` dict alongside `_active_boards`
- **Job pickup query** (`job_db.py:604`): New WHERE clause for `retry_after` with sentinel
- **Error classification** (`pipeline_orchestrator.py:638`): Three-tier classification guard
- **Provider config** (`.env.local`): workspace already targets the `openai,gemini,claude` chain; operational restart guidance remains required because workers inherit the server environment
- **Shared JSON answer generation** (`application_submit_common.py`): OpenAI structured output + bounded retry + `json_lenient` repair already exist on the submit-answer path
- **Timeout computation** (`pipeline_orchestrator.py:78`): Dynamic based on last failure_type
- **Inner rate-limit retries** (`pipeline_orchestrator.py:46`): `_RATE_LIMIT_RETRIES` 5→2
- **No UI changes** — stopped job recovery UX is out of scope

## Acceptance Criteria

### Phase 1: R3 — Three-tier error classification (bug fix, quick win)

- [x] Three-tier classification: `_NON_TRANSIENT_TYPES` (frozenset, permanent), `_TRANSIENT_PATTERNS` (tuple, transient), default (unknown/cautious) — `pipeline_orchestrator.py`
- [x] `_NON_TRANSIENT_TYPES: frozenset[str]` with: `duplicate`, `auth_failed`, `auth_guarded`, `auth_unknown`, `unsupported`, `user_rejected`, `user_stopped`, `job_closed`, `incomplete` — `pipeline_orchestrator.py`
- [x] Add `failure_type` keyword-only parameter to `_auto_retry_if_transient()` signature — `pipeline_orchestrator.py`
- [x] Guard at top of function: if `failure_type in _NON_TRANSIENT_TYPES`, return `"stopped"` immediately
- [x] For UNKNOWN errors: use a cautious retry budget (2 attempts) vs the full transient retry budget — `pipeline_orchestrator.py`
- [x] Update callers to pass `failure_type=` from stopped-job handling paths — `job_worker.py`, `pipeline_orchestrator.py`
- [x] Reduce `_RATE_LIMIT_RETRIES` from 5 to 2 — `pipeline_orchestrator.py`
- [x] Test: mock `failure_type='auth_failed'` + `error_message` containing "retry later" → confirm not requeued — `tests/test_pipeline_orchestrator.py`
- [x] Test: mock unknown error (no pattern match, no failure_type) → confirm cautious retry budget — `tests/test_pipeline_orchestrator.py`

### Phase 2: R2 + R6 — Pipeline backoff + board cooldown (designed together)

**R2: `retry_after` column and backoff**

- [x] Migration adds `retry_after TIMESTAMP NOT NULL DEFAULT '1970-01-01 00:00:00'` to `jobs` table — `job_db.py` `_MIGRATIONS`
- [x] Backfill existing rows: `UPDATE jobs SET retry_after = '1970-01-01 00:00:00' WHERE retry_after IS NULL`
- [x] Composite index `(status, retry_after)` via `CREATE INDEX IF NOT EXISTS` in `_SCHEMA` — `job_db.py`
- [x] `get_pending_jobs()` adds `AND retry_after <= datetime('now')` — `job_db.py`
- [x] Fixed delay table replaces exponential formula:
  ```python
  _RETRY_DELAYS = [120, 480, 1800]  # 2min, 8min, 30min — matches origin spec
  delay = _RETRY_DELAYS[min(fix_attempts, len(_RETRY_DELAYS) - 1)]
  delay += random.uniform(0, 0.25 * delay)  # full jitter (AWS-recommended)
  ```
- [x] Compute `retry_after` via SQLite: `datetime('now', '+{delay} seconds')` for UTC consistency — `pipeline_orchestrator.py`
- [x] CAS guard: requeue SQL adds `AND status IN (...)` to WHERE clause — `pipeline_orchestrator.py`
- [x] Requeue SQL sets `retry_after`, `status='queued'`, `provider=NULL` — `pipeline_orchestrator.py`
- [x] Every other requeue path clears `retry_after` to sentinel `'1970-01-01 00:00:00'`:
  - Kill-worker requeue — `job_worker.py`
  - `reset_stale_jobs()` — `job_db.py`
  - TUI/web stop-workers — `job_tui.py`, `job_web.py`
  - `regenerate_job()` — `job_db.py`
- [x] Safety-net in `reset_stale_jobs()`: cap far-future `retry_after` values back to the sentinel for queued jobs — `job_db.py`
- [x] Test: requeue a job 3× → verify `retry_after` timestamps are ~2min, ~8min, ~30min from requeue time — `tests/test_pipeline_orchestrator.py`

**R6: Board cooldown**

- [x] `_board_cooldowns: dict[str, datetime]` with `threading.Lock` on `WorkerPool` — `job_worker.py`
- [x] `set_board_cooldown(board: str)` — sets `cooldown_until = now + BOARD_COOLDOWN_SECONDS` — `job_worker.py`
- [x] `is_board_rate_limited(board: str) -> bool` — returns True if `cooldown_until` is in future — `job_worker.py`
- [x] `BOARD_COOLDOWN_SECONDS` env var, default 300 (5 min) — `job_worker.py`
- [x] `_is_rate_limit_error(error_message: str) -> bool` — rate-limit-specific patterns only (`"rate limit"`, `"429"`, `"too many requests"`, `"ratelimit"`) — `pipeline_orchestrator.py`
- [x] When `_is_rate_limit_error()` returns True, call `WorkerPool.set_board_cooldown()` for that board — `job_worker.py`
- [x] `Coordinator.next_job()` skips jobs whose board is rate-limited; jobs with `board IS NULL` are never blocked — `job_worker.py`
- [x] Distinct log messages separate board cooldown skips from active-worker board locks — `job_worker.py`
- [x] Test: rate-limit one Greenhouse job → verify all queued Greenhouse jobs are delayed by cooldown — `tests/test_job_worker.py`

### Phase 3: R1 + R4 + R5 — Provider chain + structured output + timeout escalation (parallel)

**R1: Enable provider fallback chain (config + verification)**

- [ ] Verify `gemini` binary is installed and responds to `--help` or equivalent
- [x] Verify `llm_common.sh` case statement accepts `openai`, `gemini`, `claude`
- [x] Verify `llm_provider.py` `VALID_PROVIDERS` includes all three
- [x] Workspace `.env.local` currently targets `openai,gemini,claude` as the intended chain baseline
- [ ] Test: run a single job with OpenAI intentionally failing → verify Gemini fallback triggers
- [x] Document in `.env.local` comments: restart web server after changing provider chain or cooldown values

**R4: Structured output for JSON reliability**

- [ ] Decide which remaining JSON-producing asset-generation seams still need strict-schema treatment beyond the already-upgraded shared submit-answer path
- [x] Keep `json_lenient.py` as repair fallback for providers/modes without structured output
- [ ] Track repair rates: log which `repair_step` fires per provider — if >5% need repair, investigate
- [x] If structured output is not feasible for a specific mode, inline a single retry at the JSON parse call site: catch `JSONDecodeError`, re-invoke same phase, let it raise on second failure — `application_submit_common.py`
- [ ] Test: verify any newly-upgraded asset-generation path produces valid JSON with the chosen strict contract

**R5: Timeout escalation on retry**

- [x] `_escalated_timeout(base_timeout: int, last_failure_type: str | None) -> int` — `pipeline_orchestrator.py`
  - If `last_failure_type == 'timeout'`: return `min(int(base_timeout * 1.5), base_timeout * 2)`
  - Otherwise: return `base_timeout` (no escalation for non-timeout retries)
- [x] `_run_phases_1_2()` reads previous `failure_type` from job row, passes to `_escalated_timeout` — `pipeline_orchestrator.py`
- [x] Cap at 2× `DEFAULT_ASSET_TIMEOUT` (default: 1800s). Per-provider timeout.
- [x] Test: job with previous `failure_type='timeout'` → verify escalated timeout; job with `failure_type='retries_exhausted'` → verify default timeout — `tests/test_pipeline_orchestrator.py`

## Success Metrics

(see origin: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`)

- Stopped job rate for new batches drops below 25% (from current 54%)
- Zero wasted retries on duplicate/auth failures (R3)
- LLM generation failures drop by ≥50% due to fallback chain (R1)
- Rate-limit stops drop by ≥50% due to backoff + cooldown (R2, R6)

## Dependencies & Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Stale `retry_after` permanently hides jobs | Medium | Jobs lost forever | Safety-net cap in `reset_stale_jobs`, sentinel date, requeue-path audit |
| Retry storm (36+ API calls per job) | Medium | Provider rate limiting | `_RATE_LIMIT_RETRIES` 5→2, board cooldown, document total budget |
| Gemini provider not installed or broken | Medium | R1 degrades to 2-provider chain | Verify before enabling; chain degrades gracefully |
| Structured output not available in all modes | Medium | R4 partial | Keep `json_lenient.py` as fallback |
| CAS race in `_auto_retry_if_transient` | Low | Job status overwritten | Add `AND status IN (...)` guard |
| Board cooldown too aggressive | Low | Jobs delayed unnecessarily | Configurable via env var |
| Shell/Python provider validation divergence | Medium | Chain config rejected by one layer | Verify both layers in Phase 3 |

## Implementation Order & Dependencies

```
Phase 1 (R3) ──── foundation already present in repo; preserve as a regression contract
     │
Phase 2 (R2+R6) ─ already present in repo; keep reset/requeue parity and safety-net coverage intact
     │
Phase 3 (R1+R4+R5) ── active remaining work: provider-chain verification, JSON-scope decision, and final resilience validation
```

## Key Files

| File | Changes |
|------|---------|
| `scripts/pipeline_orchestrator.py` | R2 backoff delays, R3 three-tier classification, R5 timeout escalation, R6 rate-limit detection, reduce `_RATE_LIMIT_RETRIES` |
| `scripts/job_db.py` | R2 migration + sentinel + index + query filter, safety-net cap |
| `scripts/job_worker.py` | R2 `retry_after` awareness in coordinator, R6 `_board_cooldowns` on WorkerPool |
| `scripts/job_web.py` | R2 clear `retry_after` on stop-workers/requeue |
| `scripts/job_tui.py` | R2 clear `retry_after` on stop-workers/requeue |
| `scripts/application_submit_common.py` | Current R4 pattern: OpenAI JSON schema, one-shot retry, `json_lenient` repair, provider observability headers |
| `scripts/run_pipeline.py` | Potential R4 follow-on only if stopped-job evidence shows remaining malformed JSON on asset-generation paths |
| `scripts/build_resume.py` | Potential R4 follow-on if resume-content parsing is still a contributor to stopped jobs |
| `scripts/llm_provider.py` | R1 verification (VALID_PROVIDERS), R4 structured output flags |
| `scripts/llm_common.sh` | R1 verification (case statement) |
| `.env.local` | R1 chain config, R6 cooldown config |
| `tests/test_pipeline_orchestrator.py` | Regression coverage for retry classification, retry budgets, backoff delays, and timeout escalation |
| `tests/test_job_db.py` | Regression coverage for `retry_after` filtering and stale-job safety-net behavior |
| `tests/test_job_worker.py` | Regression coverage for board cooldown skip behavior and failure-type propagation |
| `tests/test_submit_application.py` | Regression coverage for structured-output submit answers and JSON retry behavior |
| `tests/test_openai_provider.py` | OpenAI JSON-schema command-builder coverage |

## Sources & References

### Origin
- **Origin document:** [../brainstorms/2026-03-25-pipeline-resilience-requirements.md](../brainstorms/2026-03-25-pipeline-resilience-requirements.md) — Key decisions: provider chain order (openai→gemini→claude), exponential backoff at pipeline level, board-wide cooldown

### Internal References
- **Prior plan:** [2026-03-24-011-fix-involuntary-job-failures-resilience-plan.md](2026-03-24-011-fix-involuntary-job-failures-resilience-plan.md) — Predecessor resilience work
- **Solution: adding-new-llm-provider.md** — Requeue must clear provider=NULL, env inheritance
- **Solution: strict-submit-answer-schema-requires-nullable-optionals.md** — Existing structured-output pattern on the shared submit-answer path
- **Solution: fragile-question-classifier** — Priority-ordered dispatch prevents misclassification
- **Spec: gemini-provider-fallback-chain-design.md** — Existing fallback chain and partial-success detection

### External References (from best practices research)
- [AWS: Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) — Full jitter recommended for contention scenarios
- [Thom Wright: Decorrelated Jitter Clamping Problem (2024)](https://thomwright.co.uk/2024/04/24/decorrelated-jitter/) — Why decorrelated jitter breaks at cap
- [SkyPilot: SQLite Concurrency at Scale](https://blog.skypilot.co/abusing-sqlite-to-handle-concurrency/) — busy_timeout tuning for high-thread workloads
- [Bert Hubert: SQLITE_BUSY Despite Timeout](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/) — Transaction upgrade bypass
- [Anthropic: Claude Structured Outputs (GA)](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) — JSON schema constrained decoding
- [Temporal: Error Handling in Distributed Systems](https://temporal.io/blog/error-handling-in-distributed-systems) — Hybrid error classification pattern
