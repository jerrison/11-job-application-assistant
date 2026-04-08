---
title: "fix: CI unit-test mock targets wrong module and lever file unformatted"
type: fix
status: completed
date: 2026-03-24
---

# fix: CI unit-test mock targets wrong module and lever file unformatted

## Overview

CI on the `fix/lever-empty-fields-compensation-pronouns-age-nda` PR (#31) fails two jobs:
- **unit-tests**: 2 failures in `test_autofill_pipeline.py`
- **lint**: `scripts/autofill_lever.py` needs ruff reformatting

Same failures also appeared on the earlier `fix/confirmation-email-reply` PR (#30). Both are pre-existing issues on `main`.

## Problem Statement

### Unit test failures

`test_payload_only_writes_json_and_returns_zero` and `test_no_browser_board_skips_playwright` both fail in CI with:

```
RuntimeError: No answer-generation provider found. Install `gemini`, `claude`, or `codex`, or set ASSET_LLM_PROVIDER.
```

**Root cause:** Commit `0a7b9206` added mocks for `default_answer_provider`, but used the wrong target:

```python
# Current (WRONG) — patches the source module, not the imported reference
mock.patch("application_submit_common.default_answer_provider", return_value="claude")
```

`autofill_pipeline.py` does `from application_submit_common import default_answer_provider`, which binds the function into its own namespace. Patching the source module doesn't affect the already-imported name. This is the classic Python mock-where-it's-used gotcha.

**Fix:**
```python
# Correct — patches where the name is actually looked up
mock.patch.object(pipeline, "default_answer_provider", return_value="claude")
```

Tests pass locally because `claude` CLI is on PATH, masking the broken mock.

### Lint failure

`scripts/autofill_lever.py` needs reformatting. The lever fix PR modified this file but didn't run `ruff format`.

## Acceptance Criteria

- [ ] Fix mock targets in `tests/test_autofill_pipeline.py` (lines 49 and 80): change `mock.patch("application_submit_common.default_answer_provider", ...)` to `mock.patch.object(pipeline, "default_answer_provider", ...)`
- [ ] Run `uv run ruff format scripts/autofill_lever.py`
- [ ] Verify `uv run python -m pytest tests/test_autofill_pipeline.py::AutofillMainTests -v` passes with `ASSET_LLM_PROVIDER` unset and no provider CLIs on PATH (simulate CI)
- [ ] Verify `uv run ruff format --check scripts/ tests/` passes
- [ ] Push to main and confirm CI goes green

## Context

- CI run: `gh run view 23525252080`
- Prior fix attempt: commit `0a7b9206`
- Affected files: `tests/test_autofill_pipeline.py`, `scripts/autofill_lever.py`
