# Research Cache Invalidation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 7-day TTL research cache with a two-tier system: company-level cache (30-day configurable TTL) and role-level cache (JD-hash + TTL invalidation).

**Architecture:** Split the monolithic research prompt into company research (shared across roles) and role research (per-JD). Add `role_research_cache.json` keyed by SHA-256 of `jd_parsed.json`. Company cache stays at `research_cache.json`. Drafting and answer-generation prompts read from both files.

**Tech Stack:** Bash (shell functions), Python 3 (inline scripts), SHA-256 hashing

**Spec:** `docs/superpowers/specs/2026-03-13-research-cache-invalidation-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/llm_common.sh` | Modify | Cache freshness functions, research/drafting prompt templates |
| `scripts/llm_worker.sh` | Modify | Worker orchestration with two-tier cache logic |
| `apply.sh` | Modify | Single-job orchestration with two-tier cache logic |
| `scripts/application_submit_common.py` | Modify | Submit-time answer generation reads both caches |
| `scripts/autofill_greenhouse.py` | Modify | Greenhouse answer generation reads both caches |
| `tests/test_llm_common.py` | Modify | Update prompt tests for split prompts |
| `tests/test_research_cache.py` | Create | New tests for role cache freshness |
| `AGENTS.md` | Modify | Update TTL/cache references |
| `GEMINI.md` | Modify | Sync with AGENTS.md |
| `CLAUDE.md` | Modify | Add cache architecture note |
| `.github/copilot-instructions.md` | Modify | Add cache architecture note |

---

## Chunk 1: Cache Freshness Functions

### Task 1: Update `job_assets_cache_is_fresh` TTL default and add `job_assets_role_cache_is_fresh`

**Files:**
- Modify: `scripts/llm_common.sh:125-144`
- Create: `tests/test_research_cache.py`

- [ ] **Step 1: Write failing tests for `job_assets_role_cache_is_fresh`**

```python
# tests/test_research_cache.py
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_shell(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-lc", f"source scripts/llm_common.sh; {script}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=merged_env,
    )


class CacheIsFreshTests(unittest.TestCase):
    def test_default_ttl_is_30_days(self):
        """job_assets_cache_is_fresh with no explicit TTL arg uses 30 days."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            f.write(b'{}')
            f.flush()
            # File just created — should be fresh with 30-day default
            result = run_shell(f'job_assets_cache_is_fresh "{f.name}"')
            self.assertEqual(result.returncode, 0)

    def test_env_var_overrides_default_ttl(self):
        """JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS overrides the 30-day default."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            f.write(b'{}')
            f.flush()
            # Set TTL to 0 — file should be stale immediately
            result = run_shell(
                f'job_assets_cache_is_fresh "{f.name}"',
                env={"JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS": "0"},
            )
            self.assertEqual(result.returncode, 1)


class RoleCacheIsFreshTests(unittest.TestCase):
    def test_fresh_when_hash_matches_and_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')
            jd_hash = hashlib.sha256(jd_path.read_bytes()).hexdigest()

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(json.dumps({
                "role_context": "test",
                "researched_at": "2026-03-13T00:00:00Z",
                "jd_hash": jd_hash,
            }))

            result = run_shell(
                f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30'
            )
            self.assertEqual(result.returncode, 0)

    def test_stale_when_hash_differs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(json.dumps({
                "role_context": "test",
                "researched_at": "2026-03-13T00:00:00Z",
                "jd_hash": "wrong_hash_value",
            }))

            result = run_shell(
                f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30'
            )
            self.assertEqual(result.returncode, 1)

    def test_stale_when_role_cache_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')

            result = run_shell(
                f'job_assets_role_cache_is_fresh "{tmpdir}/nonexistent.json" "{jd_path}" 30'
            )
            self.assertEqual(result.returncode, 1)

    def test_stale_when_jd_parsed_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(json.dumps({
                "role_context": "test",
                "jd_hash": "abc123",
            }))

            result = run_shell(
                f'job_assets_role_cache_is_fresh "{role_cache}" "{tmpdir}/missing_jd.json" 30'
            )
            self.assertEqual(result.returncode, 1)

    def test_stale_when_over_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')
            jd_hash = hashlib.sha256(jd_path.read_bytes()).hexdigest()

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(json.dumps({
                "role_context": "test",
                "jd_hash": jd_hash,
            }))
            # Set mtime to 31 days ago
            old_time = time.time() - (31 * 86400)
            os.utime(role_cache, (old_time, old_time))

            result = run_shell(
                f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30'
            )
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_research_cache.py -v`
Expected: FAIL — `job_assets_role_cache_is_fresh` not defined, TTL default is still 7

- [ ] **Step 3: Update `job_assets_cache_is_fresh` default TTL**

In `scripts/llm_common.sh`, change line 127:

```bash
# Before:
local max_age_days="${2:-7}"

# After:
local max_age_days="${2:-${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}}"
```

- [ ] **Step 4: Implement `job_assets_role_cache_is_fresh`**

Add after `job_assets_cache_is_fresh` (after line 144) in `scripts/llm_common.sh`:

```bash
job_assets_role_cache_is_fresh() {
    local role_cache_path="$1"
    local jd_parsed_path="$2"
    local max_age_days="${3:-${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}}"
    python3 - "$role_cache_path" "$jd_parsed_path" "$max_age_days" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

role_cache_path = sys.argv[1]
jd_parsed_path = sys.argv[2]
max_age_days = int(sys.argv[3])

if not os.path.exists(role_cache_path):
    raise SystemExit(1)

if not os.path.exists(jd_parsed_path):
    raise SystemExit(1)

age_seconds = time.time() - os.path.getmtime(role_cache_path)
if age_seconds > max_age_days * 86400:
    raise SystemExit(1)

with open(jd_parsed_path, "rb") as f:
    current_hash = hashlib.sha256(f.read()).hexdigest()

with open(role_cache_path, encoding="utf-8") as f:
    stored_hash = json.load(f).get("jd_hash", "")

raise SystemExit(0 if current_hash == stored_hash else 1)
PY
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_research_cache.py -v`
Expected: All PASS

- [ ] **Step 6: Run all tests**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add scripts/llm_common.sh tests/test_research_cache.py
git commit -m "feat: add configurable TTL and role cache freshness check"
```

## Chunk 2: Research Prompt Split

### Task 2: Split research prompt into company and role prompts

**Files:**
- Modify: `scripts/llm_common.sh:146-180` (research prompt)
- Modify: `scripts/llm_common.sh:182-230` (drafting prompt)
- Modify: `tests/test_llm_common.py`

- [ ] **Step 1: Split `job_assets_write_research_prompt` into company and role prompts**

Rename existing `job_assets_write_research_prompt` to `job_assets_write_company_research_prompt`. Change its output instruction from:

```
4. SAVE the research only to output/${company}/research_cache.json
```

to:

```
4. SAVE the company-level research to output/${company}/research_cache.json as structured JSON.
   Include: company, researched_at, mission, vision, culture, leadership, product, growth, recent_news, tech_stack.
   Do NOT include role_context — that is handled separately.
```

Add new function `job_assets_write_role_research_prompt`:

```bash
job_assets_write_role_research_prompt() {
    local prompt_file="$1"
    local company="$2"
    local role="$3"
    local out_dir="$4"
    local content_dir="${5:-$4}"
    local jd_hash="$6"

    cat > "$prompt_file" <<PROMPT
You are a Job Application Agent. Follow the instructions in AGENTS.md exactly.

The company research for ${company} is already saved. Now do role-specific research.

These files exist:
- ${content_dir}/jd_parsed.json (parsed JD)
- output/${company}/research_cache.json (company-level research — read this for context)

Your task:

1. READ ${content_dir}/jd_parsed.json to understand the specific role.
2. READ output/${company}/research_cache.json for company context.
3. Do role-specific research: how this role fits the company strategy, team structure, what skills matter most for this position, competitive landscape.
4. SAVE the role research to ${content_dir}/role_research_cache.json as JSON with these fields:
   - role_context: detailed role-specific research
   - researched_at: current ISO 8601 timestamp
   - jd_hash: "${jd_hash}"

IMPORTANT:
- Do NOT re-research company-level information (mission, culture, leadership). That is already in research_cache.json.
- Do NOT tailor the resume or write the cover letter in this step.
- Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py, or any other repo automation entrypoint.
- Do NOT show the research in chat — just confirm the file is saved.
PROMPT
}
```

- [ ] **Step 2: Update `job_assets_write_drafting_prompt` to read both caches**

In `scripts/llm_common.sh`, update the drafting prompt's file list and instructions. Change:

```
- output/${company}/research_cache.json (company and role research from the prior research step)
```

to:

```
- output/${company}/research_cache.json (company-level research)
- ${content_dir}/role_research_cache.json (role-specific research — if this file exists, use it for role context; otherwise fall back to role_context in research_cache.json)
```

And change instruction 3 from:

```
3. READ output/${company}/research_cache.json and use it as the research input for both the resume and the cover letter.
```

to:

```
3. READ output/${company}/research_cache.json for company research.
4. READ ${content_dir}/role_research_cache.json for role-specific research (if it exists; otherwise use role_context from research_cache.json).
5. Use the combined company + role research as input for both the resume and the cover letter.
```

Renumber subsequent instructions accordingly.

- [ ] **Step 3: Update tests in `tests/test_llm_common.py`**

Update `test_research_prompt_forbids_recursive_repo_entrypoints`:
- Change function name from `job_assets_write_research_prompt` to `job_assets_write_company_research_prompt`
- Update assertion: change `"SAVE the research only to output/starburst/research_cache.json"` to assert the prompt mentions `research_cache.json` and does NOT mention `role_context`

Add new test `test_role_research_prompt_writes_to_role_cache`:
```python
def test_role_research_prompt_writes_to_role_cache(self):
    completed = subprocess.run(
        [
            "bash",
            "-lc",
            "source scripts/llm_common.sh; prompt=$(mktemp); "
            "job_assets_write_role_research_prompt \"$prompt\" starburst senior-pm output/starburst output/starburst/content abc123hash; "
            "cat \"$prompt\"",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    self.assertIn("role_research_cache.json", completed.stdout)
    self.assertIn("abc123hash", completed.stdout)
    self.assertIn("Do NOT re-research company-level information", completed.stdout)
```

Update `test_drafting_prompt_reads_research_cache_and_forbids_recursive_repo_entrypoints`:
- The existing assertion on line 50 (`"READ output/starburst/research_cache.json and use it as the research input"`) will break because the prompt text changes. Replace it with:
  ```python
  self.assertIn("READ output/starburst/research_cache.json for company research", completed.stdout)
  ```
- Add new assertion for role cache:
  ```python
  self.assertIn("role_research_cache.json", completed.stdout)
  ```

- [ ] **Step 4: Run all tests**

Run: `uv run python -m pytest tests/test_llm_common.py tests/test_research_cache.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/llm_common.sh tests/test_llm_common.py
git commit -m "feat: split research prompt into company and role prompts"
```

## Chunk 3: Worker & Apply Orchestration

### Task 3: Update `llm_worker.sh` with two-tier cache logic

**Files:**
- Modify: `scripts/llm_worker.sh`

- [ ] **Step 1: Add role cache variables and update TTL**

After line 23 (`research_cache=...`), add:

```bash
role_research_cache="${PROJECT_ROOT}/${content_dir}/role_research_cache.json"
jd_parsed="${PROJECT_ROOT}/${content_dir}/jd_parsed.json"
cache_ttl_days="${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}"
```

Change line 45 from:
```bash
if job_assets_cache_is_fresh "$research_cache" 7; then
```
to:
```bash
if job_assets_cache_is_fresh "$research_cache" "$cache_ttl_days"; then
```

- [ ] **Step 2: Add role cache check after company cache loop**

After the research cache existence guard (line 78 — `fi` closing the `if [[ ! -f "$research_cache" ]]` block), before the drafting section (line 80), add role cache check. The role research prompt reads `research_cache.json`, so this must come after confirming it exists:

```bash
# Check role-specific cache
if ! job_assets_role_cache_is_fresh "$role_research_cache" "$jd_parsed" "$cache_ttl_days"; then
    echo "[$(date '+%H:%M:%S')] ROLE_RESEARCH ${company}/${role} (role-specific research needed)"
    prompt_file="$(mktemp)"
    jd_hash="$(job_assets_file_sha256 "$jd_parsed")"
    job_assets_write_role_research_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir" "$jd_hash"
    role_research_log="${content_dir}/llm_role_research_raw.txt"
    if ! job_assets_run_prompt_with_fallback "$provider" "$prompt_file" research "$role_research_log" "$role_research_cache"; then
        echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, role research error)"
        exit 1
    fi
    rm -f "$prompt_file"
    prompt_file=""
else
    echo "[$(date '+%H:%M:%S')] CACHE  ${company}/${role} (fresh role research cache)"
fi
```

- [ ] **Step 3: Update company research to use renamed function**

Change line 54 from:
```bash
job_assets_write_research_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir"
```
to:
```bash
job_assets_write_company_research_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir"
```

- [ ] **Step 4: Run all tests**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/llm_worker.sh
git commit -m "feat: add two-tier cache logic to llm_worker.sh"
```

### Task 4: Update `apply.sh` with two-tier cache logic

**Files:**
- Modify: `apply.sh:205-237`

- [ ] **Step 1: Add role cache variables and update TTL**

After line 206 (`RESEARCH_CACHE=...`), add:

```bash
ROLE_RESEARCH_CACHE="${CONTENT_DIR}/role_research_cache.json"
JD_PARSED="${CONTENT_DIR}/jd_parsed.json"
CACHE_TTL_DAYS="${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}"
```

Change line 211 from:
```bash
if job_assets_cache_is_fresh "$RESEARCH_CACHE" 7; then
```
to:
```bash
if job_assets_cache_is_fresh "$RESEARCH_CACHE" "$CACHE_TTL_DAYS"; then
```

- [ ] **Step 2: Update company research to use renamed function**

Change line 217 from:
```bash
job_assets_write_research_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR"
```
to:
```bash
job_assets_write_company_research_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR"
```

- [ ] **Step 3: Add role cache check after company research block**

After the company research block (after line 237, `fi`), before "Step 2b: LLM resume and cover letter drafting", add:

```bash
    if ! job_assets_role_cache_is_fresh "$ROLE_RESEARCH_CACHE" "$JD_PARSED" "$CACHE_TTL_DAYS"; then
        echo "── Step 2a-role: LLM role-specific research ──"
        PROMPT_FILE="$(mktemp)"
        JD_HASH="$(job_assets_file_sha256 "$JD_PARSED")"
        job_assets_write_role_research_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR" "$JD_HASH"
        ROLE_RESEARCH_LOG="${CONTENT_DIR}/llm_role_research_raw.txt"
        if ! job_assets_run_prompt_with_fallback \
            "$PROVIDER" \
            "$PROMPT_FILE" \
            research \
            "$ROLE_RESEARCH_LOG" \
            "$ROLE_RESEARCH_CACHE"; then
            rm -f "$PROMPT_FILE"
            echo "ERROR: LLM role-specific research failed. See ${ROLE_RESEARCH_LOG}" >&2
            exit 1
        fi
        rm -f "$PROMPT_FILE"
    else
        echo "── Step 2a-role: Reusing existing role research cache ──"
    fi
```

- [ ] **Step 4: Run all tests**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add apply.sh
git commit -m "feat: add two-tier cache logic to apply.sh"
```

## Chunk 4: Answer Generation & Docs

### Task 5: Update `application_submit_common.py` to read both caches

**Files:**
- Modify: `scripts/application_submit_common.py:824`

- [ ] **Step 1: Update `generate_application_answers` to load role cache**

At line 824, after loading `research_cache`:

```python
# Before:
research_cache = load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json")

# After:
research_cache = load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json") or {}
role_research = load_optional_json(role_content_path(out_dir, "role_research_cache.json")) or load_optional_json(out_dir / "role_research_cache.json")
if role_research and "role_context" in role_research:
    research_cache = {**research_cache, "role_context": role_research["role_context"]}
```

This merges `role_context` from the role cache into the research cache dict before passing it to the prompt builder. The prompt builder doesn't change — it just sees `role_context` in the dict as before.

- [ ] **Step 2: Run all tests**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add scripts/application_submit_common.py
git commit -m "feat: read role_research_cache.json in submit answer generation"
```

### Task 6: Update `autofill_greenhouse.py` to read both caches

**Files:**
- Modify: `scripts/autofill_greenhouse.py:2216`

- [ ] **Step 1: Update `_generate_application_answers` to load role cache**

At line 2216, same pattern as Task 5:

```python
# Before:
research_cache = _load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json")

# After:
research_cache = _load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json") or {}
_role_research = _load_optional_json(role_content_path(out_dir, "role_research_cache.json")) or _load_optional_json(out_dir / "role_research_cache.json")
if _role_research and "role_context" in _role_research:
    research_cache = {**research_cache, "role_context": _role_research["role_context"]}
```

- [ ] **Step 2: Run Greenhouse tests**

Run: `uv run python -m pytest tests/test_greenhouse_autofill.py -v`
Expected: All pass

- [ ] **Step 3: Run all tests**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add scripts/autofill_greenhouse.py
git commit -m "feat: read role_research_cache.json in Greenhouse answer generation"
```

### Task 7: Update agent context files

**Files:**
- Modify: `AGENTS.md`
- Modify: `GEMINI.md`
- Modify: `CLAUDE.md`
- Modify: `.github/copilot-instructions.md`

- [ ] **Step 1: Update AGENTS.md**

Search for all references to `7` day cache and `research_cache.json` as the single research output. Key locations to update:

1. Any reference to "7-day" research cache → change to "configurable TTL (default 30 days, override with `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS`)"
2. References to saving research "only to `output/{company}/research_cache.json`" → add mention that role-specific research goes to `{content_dir}/role_research_cache.json`
3. The research prompt template instructions (if present in AGENTS.md) → update to reflect two-tier cache

Use `grep -n "7.*day\|research_cache" AGENTS.md` to find exact lines.

- [ ] **Step 2: Sync GEMINI.md with AGENTS.md**

Apply the same changes to `GEMINI.md`. These files must stay in sync.

- [ ] **Step 3: Update CLAUDE.md and copilot-instructions.md**

Add under the existing architecture section:

```markdown
## Research Cache Architecture

Two-tier cache for company/role research:

- **Company cache** — `output/{company}/research_cache.json` — shared across roles, 30-day configurable TTL
- **Role cache** — `output/{company}/{role}/content/role_research_cache.json` — per-JD (keyed by SHA-256 hash of `jd_parsed.json`), same TTL
- **Config:** `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS` env var (default 30, set to 0 to force re-research)
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md GEMINI.md CLAUDE.md .github/copilot-instructions.md
git commit -m "docs: update agent context files for two-tier research cache"
```

## Chunk 5: Verification

### Task 8: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify backward compatibility**

Check that the drafting prompt still references `research_cache.json` as a fallback:

Run: `bash -lc "source scripts/llm_common.sh; prompt=\$(mktemp); job_assets_write_drafting_prompt \"\$prompt\" testco pm output/testco output/testco/content; grep -c 'role_research_cache\|research_cache' \"\$prompt\""`
Expected: Both filenames mentioned

- [ ] **Step 3: Verify role cache freshness function works end-to-end**

```bash
# Create a test jd_parsed.json, compute its hash, create a matching role cache, verify fresh
tmpdir=$(mktemp -d)
echo '{"title":"PM"}' > "$tmpdir/jd_parsed.json"
hash=$(shasum -a 256 "$tmpdir/jd_parsed.json" | awk '{print $1}')
echo "{\"role_context\":\"test\",\"jd_hash\":\"$hash\"}" > "$tmpdir/role_cache.json"
bash -lc "source scripts/llm_common.sh; job_assets_role_cache_is_fresh '$tmpdir/role_cache.json' '$tmpdir/jd_parsed.json' 30 && echo FRESH || echo STALE"
# Expected: FRESH

# Change JD, verify stale
echo '{"title":"Engineer"}' > "$tmpdir/jd_parsed.json"
bash -lc "source scripts/llm_common.sh; job_assets_role_cache_is_fresh '$tmpdir/role_cache.json' '$tmpdir/jd_parsed.json' 30 && echo FRESH || echo STALE"
# Expected: STALE
rm -rf "$tmpdir"
```

- [ ] **Step 4: Push to GitHub**

```bash
git push origin main
```
