---
title: "fix: Confirmation email reply not sent for LinkedIn and Greenhouse jobs (+ broken fallbacks)"
type: fix
status: completed
date: 2026-03-24
deepened: 2026-03-24
---

# fix: Confirmation email reply not sent for LinkedIn and Greenhouse jobs (+ broken fallbacks)

## Enhancement Summary

**Deepened on:** 2026-03-24
**Review agents used:** Python reviewer, pattern-recognition specialist, code-simplicity reviewer, architecture-strategist, security-sentinel, performance-oracle

### Key Improvements from Review
1. **Architecture redesign**: Make `reply_to_confirmation_email` self-sufficient instead of enriching orchestrator payload — read company from `.pipeline_meta.json`, derive all artifact paths from `board_file_constants`
2. **Bug found in existing code**: Report fallback uses wrong key (`"report_markdown"` vs `"report_md"`) — existing fallback is dead code
3. **Bug in original plan**: `Path("").exists()` returns `True` (it's the cwd) — fallback would never trigger
4. **Simplification**: Two-attempt company search (full name → first word) instead of N progressive calls
5. **Security**: Quote company names in Gmail search to prevent operator injection
6. **Performance**: Simplify retry loop from 10/30/60s escalation to single 30s wait

### New Bugs Discovered
- `application_submit_common.py:732` — `constants.get("report_markdown", "")` uses wrong key; `board_file_constants` returns `"report_md"`. Report fallback has never worked.
- `application_submit_common.py:725-726` — `Path(artifacts.get("pre_submit_screenshot", ""))` when key is missing → `Path("")` → resolves to cwd → `.exists()` returns `True` → skips fallback silently.

---

## Overview

After submitting applications, the system should reply to the company's confirmation email (reply-to-self) with the autofill report, pre-submit screenshot, and attached resume/cover letter PDFs. This reply is not being sent for NinjaTrader (LinkedIn), ZoomInfo (Greenhouse), and Zoox (Lever) due to compounding bugs in the email reply pipeline.

## Problem Statement

Investigation confirmed all three companies sent confirmation emails to Gmail. The system failed to reply because:

1. **Company name too specific in Gmail search** — `_search_confirmation_email` strips legal suffixes (LLC, Inc) but keeps descriptive words like "Technologies". Gmail AND-matches all words, so "ZoomInfo Technologies" returns 0 results when the email only says "ZoomInfo".
2. **LinkedIn jobs stored with `company="Linkedin"`** — `promote_candidate` in `job_discovery.py` calls `add_job(conn, url)` without passing `company` from the candidate record.
3. **LinkedIn autofill never calls `reply_to_confirmation_email`** — LinkedIn is the **only** board (of 19) that doesn't call it, either directly or via the generic pipeline. Lever is covered via `run_browser_pipeline`.
4. **`reply_to_confirmation_email` is not self-sufficient** — When called with a minimal payload (`{"out_dir": ...}`), it cannot derive company name or artifact paths. The orchestrator fallback exposes this.
5. **Artifact path fallbacks are broken** — Report fallback uses wrong key (`"report_markdown"` vs `"report_md"`). Screenshot has no fallback at all. `Path("")` silently resolves to cwd instead of triggering fallback.

## Proposed Solution

**Core architectural fix**: Make `reply_to_confirmation_email` self-sufficient with just `out_dir` + `board_name`. The function should derive all missing data from disk rather than requiring callers to pre-populate everything. This fixes the orchestrator path without coupling it to autofill payload internals.

## Technical Considerations

- **Self-sufficiency over caller enrichment** (Architecture strategist): Don't have the orchestrator load payload JSON — that couples the orchestration layer to autofill data formats. The function already has `out_dir` and `board_name`; derive everything from those.
- **`.pipeline_meta.json` is the company source of truth** (Architecture strategist): Every board's `_build_payload` reads company from this file. The function should do the same when payload lacks it — no DB dependency needed.
- **Gmail search operator injection** (Security sentinel): Company names from scraped data could contain Gmail operators (`from:`, `has:`, etc.). Quote the search term.
- **`Path("")` footgun** (Python reviewer): `Path("").exists()` returns `True` because it resolves to cwd. All artifact lookups must guard against empty strings.
- **False-positive risk** (Pattern recognition): Single-word fallback for companies like "Applied Technologies" → "Applied" could match broadly. Mitigated by quoting the search term and the existing keyword filter (`application OR applying OR ...`). Monitor for false matches; add a minimum-word guard later if needed.
- **Retry loop interaction** (Performance oracle): Progressive search × 10/30/60s retry = up to 280s worst case. Simplify to single 30s retry.

## Acceptance Criteria

- [ ] `reply_to_confirmation_email` succeeds for all three test cases (NinjaTrader, ZoomInfo, Zoox) when called with minimal payload `{"out_dir": ...}`
- [ ] Gmail search finds confirmation emails for companies with legal/descriptive suffixes
- [ ] LinkedIn Easy Apply jobs store the actual employer name in `jobs.company`
- [ ] `reply_to_confirmation_email` derives company from `.pipeline_meta.json` when payload lacks it
- [ ] Both report and screenshot paths use correct `board_file_constants` keys
- [ ] `Path("")` is never passed to `.exists()` — empty strings handled explicitly
- [ ] No bare `except Exception: pass` — all exceptions logged at warning level
- [ ] All existing tests pass (`uv run python -m pytest tests/ -v`)
- [ ] No regressions for boards that already work (Greenhouse, iCIMS, Workday, Phenom, Dover)

---

## MVP

### Fix 1: Make `reply_to_confirmation_email` self-sufficient

**File:** `scripts/application_submit_common.py:707-740`

**The core fix.** When payload lacks company or artifacts, derive them from `out_dir` + `board_name`. Fix the `Path("")` bug and the `report_md` key mismatch.

```python
# scripts/application_submit_common.py — reply_to_confirmation_email (revised lines 720-740)

import logging
logger = logging.getLogger(__name__)

def reply_to_confirmation_email(
    payload: dict,
    *,
    board_name: str,
    email_confirmation: dict | None = None,
) -> bool:
    """Reply to the confirmation email with the pre-submit screenshot and autofill report."""
    if not shutil.which("gws"):
        logger.warning("gws CLI not found — skipping confirmation email reply.")
        return False

    out_dir = Path(payload["out_dir"])
    artifacts = payload.get("artifacts") or {}

    # --- Derive company from .pipeline_meta.json when payload lacks it ---
    company = payload.get("company_proper") or payload.get("company") or ""
    if not company:
        try:
            meta_path = out_dir / ".pipeline_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                company = meta.get("company_proper") or meta.get("company") or ""
        except Exception as exc:
            logger.warning("Failed to read pipeline meta for company: %s", exc)
    # Enrich payload in-place so _search_confirmation_email sees the derived company
    if company and not payload.get("company"):
        payload["company"] = company

    # --- Resolve report path (fix key mismatch: "report_md" not "report_markdown") ---
    report_path = _resolve_artifact_path(
        artifacts, "report_markdown", board_name, out_dir, fallback_key="report_md"
    )
    if not report_path:
        logger.warning("No autofill report found for email reply.")
        return False

    # --- Resolve screenshot path (new fallback) ---
    screenshot_path = _resolve_artifact_path(
        artifacts, "pre_submit_screenshot", board_name, out_dir, fallback_key="pre_submit_screenshot"
    )
    if not screenshot_path:
        logger.warning("No pre-submit screenshot found for email reply.")
        return False

    # ... rest of function unchanged, but replace payload company lookups
    # with the `company` variable derived above ...


def _resolve_artifact_path(
    artifacts: dict,
    artifact_key: str,
    board_name: str,
    out_dir: Path,
    *,
    fallback_key: str | None = None,
) -> Path | None:
    """Resolve an artifact path from payload artifacts or board_file_constants.

    Guards against Path("") which silently resolves to cwd.
    """
    raw = artifacts.get(artifact_key)
    if raw:
        path = Path(raw)
        if path.exists():
            return path

    # Fallback: derive from board_file_constants + role_submit_path
    from autofill_common import board_file_constants
    constants = board_file_constants(board_name)
    filename = constants.get(fallback_key or artifact_key, "")
    if filename:
        path = role_submit_path(out_dir, filename)
        if path.exists():
            return path

    return None
```

**Key changes:**
- Reads company from `.pipeline_meta.json` when payload lacks it (no DB dependency)
- `_resolve_artifact_path` helper handles both report and screenshot, guards against `Path("")`
- Report fallback uses correct key `"report_md"` from `board_file_constants`
- Screenshot gets the same fallback pattern
- All exceptions logged, not swallowed

### Fix 2: Simplify company name search with two-attempt strategy

**File:** `scripts/application_submit_common.py:664-704`

Two attempts max (full name → first word), with quoted search terms to prevent Gmail operator injection. Replace the 10/30/60s retry escalation with a single 30s wait.

```python
# scripts/application_submit_common.py — replace _search_confirmation_email

def _search_confirmation_email(payload: dict) -> str | None:
    """Search Gmail for a recent confirmation email matching the company name."""
    company = payload.get("company_proper") or payload.get("company") or ""
    if not company:
        return None
    import re as _re

    company_search = _re.sub(
        r",?\s*\b(Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?|PBC|L\.?P\.?)\b\.?",
        "", company, flags=_re.IGNORECASE,
    ).strip()
    company_search = company_search.replace("-", " ")

    # Two-attempt strategy: full name, then first word (if multi-word)
    words = company_search.split()
    candidates = [company_search]
    if len(words) > 1:
        candidates.append(words[0])

    for candidate in candidates:
        thread_id = _gmail_search_confirmation(candidate)
        if thread_id:
            return thread_id
    return None


def _gmail_search_confirmation(company_search: str) -> str | None:
    """Run a single Gmail search for confirmation emails matching company_search."""
    # Quote the company name to prevent Gmail operator injection
    safe_company = company_search.replace('"', "")
    try:
        result = subprocess.run(
            [
                "gws", "gmail", "users", "messages", "list",
                "--params",
                json.dumps({
                    "userId": "me",
                    "q": (
                        f'newer_than:1d "{safe_company}" '
                        f'(application OR applying OR received OR submission '
                        f'OR submitted OR "thank you" OR interest)'
                    ),
                    "maxResults": 3,
                }),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Gmail search failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return None
        messages = json.loads(result.stdout).get("messages", [])
        return messages[0].get("threadId") if messages else None
    except subprocess.TimeoutExpired:
        logger.warning("Gmail search timed out for: %s", safe_company)
        return None
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Gmail search parse error: %s", exc)
        return None
```

**Also simplify the retry loop** in `reply_to_confirmation_email` (lines 774-782):

```python
    # Before: sleep 10, 30, 60 (100s total)
    # After: single 30s wait (sufficient with two-attempt search)
    if not thread_id:
        import time
        logger.info("Waiting 30s for confirmation email to arrive...")
        time.sleep(30)
        thread_id = _search_confirmation_email(payload)
```

**Research insights:**
- Two attempts cover the common case (suffix stripping gets it, or first word gets it)
- Worst case: 2 calls × 15s timeout + 30s wait + 2 calls × 15s = 90s (down from 280s)
- Typical case: 1-2 calls × 1.5s = 1.5-3s
- Quoting prevents Gmail operator injection (`from:`, `has:`, etc.)
- Specific exception types instead of bare `except: pass`

### Fix 3: Store actual company name for LinkedIn Easy Apply jobs

**File:** `scripts/job_discovery.py:345` — `promote_candidate`

```python
# Before:
add_job(conn, url)

# After:
add_job(conn, url, company=candidate.get("company"), role_title=candidate.get("title"))
```

`add_job` already accepts `company` and `role_title` kwargs (confirmed in `job_db.py:379`). This is a one-line caller fix. Prevents future LinkedIn jobs from being stored with `company="Linkedin"`.

### Fix 4: Add `reply_to_confirmation_email` to LinkedIn autofill

**File:** `scripts/autofill_linkedin.py` — `_wizard_flow` after submission

LinkedIn is the only board (of 19) that doesn't call `reply_to_confirmation_email`. Add it at **both** exit points (confirmed and ambiguous), matching the defensive pattern in iCIMS/Workday/Phenom.

```python
# scripts/autofill_linkedin.py — import at module level (not inside try block)
from application_submit_common import reply_to_confirmation_email

# At confirmed exit point (~line 412-416):
try:
    reply_to_confirmation_email(payload, board_name="linkedin")
except Exception as exc:
    logger.warning("Email reply failed for LinkedIn: %s", exc)

# At ambiguous/screenshot exit point (~line 418-421):
try:
    reply_to_confirmation_email(payload, board_name="linkedin")
except Exception as exc:
    logger.warning("Email reply failed for LinkedIn: %s", exc)
```

**Key changes from original plan:**
- Import at module level, not inside try block (Python reviewer: ImportError would be silently caught)
- Use `logger.warning` not `print(..., file=sys.stderr)` (consistency with codebase)
- Cover both exit points (Pattern recognition: other Architecture B boards do this)

## Build Sequence

1. **Fix 1** (self-sufficient function + `_resolve_artifact_path` helper) — fixes the root cause: function can't derive data from disk. Also fixes `Path("")` bug and `report_md` key mismatch.
2. **Fix 2** (two-attempt company search + simplified retry) — fixes Gmail search for suffixed companies, hardens against operator injection.
3. **Fix 4** (LinkedIn reply call) — adds the missing call at both exit points.
4. **Fix 3** (company name storage) — one-line fix, prevents future LinkedIn company name issues.

## Testing

### Automated tests
```bash
uv run python -m pytest tests/ -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
```

### Manual verification — re-run email reply for the three original failures

Test with **minimal payload** (simulating orchestrator fallback):
```bash
uv run python -c "
import sys; sys.path.insert(0, 'scripts')
from application_submit_common import reply_to_confirmation_email

# ZoomInfo — tests company name search with suffix stripping
sent = reply_to_confirmation_email(
    {'out_dir': 'output/zoominfo-technologies-llc/senior-pm-context-engineering'},
    board_name='greenhouse'
)
print(f'ZoomInfo: {\"sent\" if sent else \"FAILED\"}'  )

# NinjaTrader — tests .pipeline_meta.json company derivation
sent = reply_to_confirmation_email(
    {'out_dir': 'output/linkedin/ninjatrader-hiring-principal-pm-prop-trading-in-united-states-linkedin'},
    board_name='linkedin'
)
print(f'NinjaTrader: {\"sent\" if sent else \"FAILED\"}')

# Zoox — tests Lever board with minimal payload
sent = reply_to_confirmation_email(
    {'out_dir': 'output/zoox/senior-staff-technical-pm-autonomy'},
    board_name='lever'
)
print(f'Zoox: {\"sent\" if sent else \"FAILED\"}')
"
```

### Edge cases to verify
- Company with single word name (e.g., "Zoox") — should work on first attempt
- Company with common-word name (e.g., "Applied Materials") — first-word search for "Applied" might match broadly, but quoted search + keyword filter should constrain it
- Missing `.pipeline_meta.json` — function should log warning and still attempt search with empty company (will return False gracefully)
- Missing screenshot — function should log warning and return False (not crash)

## Sources

- **Investigation thread**: This conversation — confirmed all 3 confirmation emails exist in Gmail but replies were never sent
- `scripts/application_submit_common.py:664-740` — `_search_confirmation_email` and `reply_to_confirmation_email`
- `scripts/application_submit_common.py:732` — broken `report_markdown` key (should be `report_md`)
- `scripts/autofill_common.py:20-33` — `board_file_constants` key names
- `scripts/pipeline_orchestrator.py:1706-1714` — orchestrator `_post_submit` fallback
- `scripts/autofill_linkedin.py:362-366,412-421` — LinkedIn screenshot capture and exit points
- `scripts/autofill_pipeline.py:329-426` — generic pipeline reply integration (Lever uses this)
- `docs/autofill-patterns.md:27` — company name false-positive gotcha
- `docs/solutions/integration-issues/adding-new-llm-provider.md` — stale-payload requeue pattern

### Review Agent References
- **Python reviewer**: `Path("")` bug, bare except anti-pattern, import placement
- **Architecture strategist**: Self-sufficiency over caller enrichment, `.pipeline_meta.json` as company source of truth
- **Simplicity reviewer**: Two-attempt search, drop artifact-loading from orchestrator, `report_md` key mismatch
- **Performance oracle**: Retry loop simplification, 2-word minimum floor, worst-case latency analysis
- **Security sentinel**: Gmail operator injection via company name, quote-wrapping fix
- **Pattern recognition**: LinkedIn is the only uncovered board, both exit points need coverage, `report_md` key confirmation
