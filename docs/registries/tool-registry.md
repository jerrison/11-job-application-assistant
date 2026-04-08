# Tool Registry

Document external capabilities and high-authority repo tools here. The point is
to make data access, guardrails, and approval boundaries obvious.

| Tool / system | Primary entrypoints | Data access / scope | Risk tiers | Approval authority | Guardrails |
|---------------|---------------------|---------------------|------------|--------------------|------------|
| Runtime filesystem | `scripts/app_paths.py`, `scripts/settings_store.py`, `scripts/output_layout.py` | User-owned materials, runtime env files, outputs, SQLite DB | `L1-L3` | Operator initiates settings/material changes; agent may write within the requested runtime scope | Runtime state stays outside git; stale proof must be cleared before reruns |
| Runtime policy and trace | `governance/runtime-policy.json`, `scripts/runtime_policy.py`, `scripts/runtime_trace.py` | Local policy decisions, required action metadata, and redacted JSONL audit trail under runtime home | `L1-L3` | Policy gates follow the action tier; `live_submit` still requires explicit `L3` approval | Never write raw secrets or prompt bodies; register processors explicitly at startup so trace sinks are inspectable |
| Public material fetch | `scripts/material_ingest.py` | Public URLs, Google Docs export URLs, Google Drive direct downloads | `L1` | Operator provides the source URL | Normalize links, fetch only the requested source, store imported text locally, never commit source URLs |
| LLM provider APIs | `scripts/llm_provider.py`, provider-specific shims | Job descriptions, user context, prompts, model responses | `L1-L2` | Operator supplies credentials and provider choice | Secrets live in shared settings/runtime env; automation fallback stays within the configured provider rules |
| Browser automation | `scripts/autofill_*.py`, `scripts/browser_runtime.py`, `scripts/submit_application.py` | Applicant sessions, form data, screenshots, draft proof | `L2-L3` | User request to draft/apply allows `L2`; live submit is `L3` | Fail closed at visible submit boundaries; screenshots are source of truth |
| SQLite queue state | `scripts/job_db.py` | Local job queue, worker state, result metadata | `L1-L2` | Agent may update local queue state during approved flows | Structured result files and proof remain the audit surface |
| Git | local git workflow | Repo history, local branches, commits, pushes, merges | `L0-L3` | Local commits are allowed when requested; push/merge/public release are `L3` | Verification gates, public scrub, and explicit approval before publication |

Update this registry whenever a new external integration, remote service, or
high-authority tool is introduced.
