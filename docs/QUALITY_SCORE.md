# Quality Score

Per-domain quality grading for the job application automation system. Inspired by harness engineering: grade each product domain and architectural layer, track gaps over time.

## Grading Scale

| Grade | Definition |
|-------|------------|
| **A** | Comprehensive, mechanically enforced |
| **B** | Good coverage, minor gaps |
| **C** | Functional but has known gaps |
| **D** | Minimal, needs significant work |
| **F** | Missing or broken |

## Domain Scores

| Domain | Test Coverage | Doc Coverage | Error Handling | Agent Legibility | Overall |
|--------|:---:|:---:|:---:|:---:|:---:|
| JD Parsing | B | B | B | B | **B** |
| Resume Generation | B | B | B | B | **B** |
| Cover Letter Generation | B | B | B | B | **B** |
| **Board Autofill** | | | | | |
| - Greenhouse | B | B | B | B | **B** |
| - Ashby | B | B | B | B | **B** |
| - Lever | C | C | C | C | **C** |
| - Gem | C | C | C | C | **C** |
| - Dover | C | C | C | C | **C** |
| - Workday | C | C | C | C | **C** |
| - Phenom | D | D | D | D | **D** |
| - iCIMS | D | D | D | D | **D** |
| **Infrastructure** | | | | | |
| Job Queue / Worker | B | B | B | B | **B** |
| CLI / TUI | B | B | B | B | **B** |
| Web UI | C | C | C | C | **C** |
| Notion Sync | C | C | C | C | **C** |
| Provider Management | B | B | B | B | **B** |
| **Tooling** | | | | | |
| Linting (Ruff) | B | B | — | B | **B** |
| Type Safety | F | F | F | F | **F** |
| Code Coverage (38%) | C | B | — | B | **C** |

## Known Gaps

1. **No type checking** -- no mypy or pyright configured; no CI gate to catch type-level regressions.
2. **Code coverage at 38%** -- Ruff linting and pytest-cov are configured and enforced in CI; coverage needs to climb toward 70%+.
3. **Phenom and iCIMS autofill immaturity** -- newer board integrations lack edge-case handling and test depth.
4. **Lever, Gem, Workday documentation gaps** -- board-specific patterns and gotchas are under-documented compared to Greenhouse/Ashby.
5. **Architecture validation is import-only** -- `check_architecture.py` validates import directions but not layer boundaries or module size limits.

## Last Updated

2026-03-16
