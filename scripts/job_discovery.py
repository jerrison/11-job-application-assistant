"""Job discovery: search, score, and promote candidate jobs."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import material_path
from job_normalization import company_match_variants, jd_fingerprint, normalize_company, normalize_role_title

try:
    from jobspy import scrape_jobs  # type: ignore[import-untyped]
except ImportError:
    scrape_jobs = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Path to master resume relative to project root
_RESUME_PATH = material_path("master_resume.md")

_SCORING_PROMPT = """\
Score how suitable this job is for the candidate on a scale of 0-100.

SCORING CRITERIA:
- Skills & experience match: 0-35 points
- Seniority level alignment: 0-25 points
- Industry/domain fit: 0-20 points
- Location/remote alignment: 0-10 points
- Career growth signal: 0-10 points

CANDIDATE RESUME:
{resume}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Description:
{description}

Respond with ONLY valid JSON: {{"score": <0-100>, "reason": "<1-2 sentences>"}}"""


def _jd_fingerprint(company: str, jd: str | None) -> str | None:
    """Create a stable fingerprint from company + JD for cross-source dedup.

    Delegates to the shared normalization helper for consistency.
    """
    return jd_fingerprint(company, jd)


def _is_valid_number(v: Any) -> bool:
    """Check if a value is a valid number (not None, not NaN)."""
    if v is None:
        return False
    try:
        return v == v  # NaN != NaN  # noqa: PLR0124
    except (TypeError, ValueError):
        return False


def _format_salary(row: dict[str, Any]) -> str:
    """Format min/max amount + currency + interval into readable string."""
    min_amt = row.get("min_amount")
    max_amt = row.get("max_amount")
    currency = row.get("currency") or "USD"
    interval = row.get("interval") or ""

    min_ok = _is_valid_number(min_amt)
    max_ok = _is_valid_number(max_amt)

    if not min_ok and not max_ok:
        return ""

    if min_ok and max_ok:
        salary = f"{currency} {int(min_amt):,}–{int(max_amt):,}"
    elif min_ok:
        salary = f"{currency} {int(min_amt):,}+"
    else:
        salary = f"{currency} up to {int(max_amt):,}"

    if interval:
        salary = f"{salary} / {interval}"
    return salary


def search_jobs(
    conn: sqlite3.Connection,
    search_term: str,
    location: str,
    results_wanted: int = 50,
    *,
    sources: list[str] | None = None,
    hours_old: int = 72,
) -> list[dict[str, Any]]:
    """Search for jobs via jobspy and insert results into candidate_jobs.

    Returns list of newly inserted candidate dicts (skips duplicates).
    """
    if scrape_jobs is None:
        msg = "jobspy is not installed; run: uv add jobspy"
        raise ImportError(msg)

    if sources is None:
        sources = ["linkedin", "indeed", "glassdoor"]

    log.info(
        "Searching for %r in %r via %s (results_wanted=%d, hours_old=%d)",
        search_term,
        location,
        sources,
        results_wanted,
        hours_old,
    )

    jobs_df = scrape_jobs(
        site_name=sources,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
    )

    # Build set of existing JD fingerprints for cross-source dedup.
    # Same job from different aggregators has same JD content.
    existing_rows = conn.execute(
        "SELECT jd_fingerprint FROM candidate_jobs WHERE jd_fingerprint IS NOT NULL"
    ).fetchall()
    _existing_fps: set[str] = {r["jd_fingerprint"] for r in existing_rows}

    inserted: list[dict[str, Any]] = []
    for _, row in jobs_df.iterrows():
        row_dict = row.to_dict()

        job_url = str(row_dict.get("job_url") or "").strip()
        if not job_url:
            continue

        title = str(row_dict.get("title") or "").strip()
        company = str(row_dict.get("company") or "").strip()
        if not title or not company:
            continue

        source = str(row_dict.get("site") or "unknown")
        location_val = str(row_dict.get("location") or "").strip() or None
        salary = _format_salary(row_dict) or None
        job_type = str(row_dict.get("job_type") or "").strip() or None
        job_level = str(row_dict.get("job_level") or "").strip() or None

        is_remote_raw = row_dict.get("is_remote")
        if is_remote_raw is None:
            is_remote = None
        else:
            is_remote = 1 if bool(is_remote_raw) else 0

        date_posted = row_dict.get("date_posted")
        if date_posted is not None:
            date_posted = str(date_posted)

        job_description = str(row_dict.get("description") or "").strip() or None
        company_industry = str(row_dict.get("company_industry") or "").strip() or None

        company_rating_raw = row_dict.get("company_rating")
        try:
            company_rating = float(company_rating_raw) if company_rating_raw is not None else None
        except (TypeError, ValueError):
            company_rating = None

        _app_raw = str(row_dict.get("job_url_direct") or "").strip()
        application_url = None if _app_raw.lower() in ("", "nan", "none") else _app_raw

        # Cross-source dedup: fingerprint the JD so the same job from
        # LinkedIn and Indeed (different URLs, same description) is skipped.
        jd_fp = _jd_fingerprint(company, job_description) if job_description else None
        if jd_fp and jd_fp in _existing_fps:
            log.debug("Skipping cross-source duplicate (JD match): %s @ %s", title, company)
            continue

        try:
            conn.execute(
                """
                INSERT INTO candidate_jobs
                    (source, title, company, job_url, application_url, location,
                     salary, job_type, job_level, is_remote, date_posted,
                     job_description, company_industry, company_rating, jd_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    title,
                    company,
                    job_url,
                    application_url,
                    location_val,
                    salary,
                    job_type,
                    job_level,
                    is_remote,
                    date_posted,
                    job_description,
                    company_industry,
                    company_rating,
                    jd_fp,
                ),
            )
            conn.commit()
            row_id = conn.execute("SELECT id FROM candidate_jobs WHERE job_url = ?", (job_url,)).fetchone()
            if row_id:
                candidate = dict(conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (row_id[0],)).fetchone())
                inserted.append(candidate)
                if jd_fp:
                    _existing_fps.add(jd_fp)
        except sqlite3.IntegrityError:
            # Duplicate job_url — skip
            log.debug("Skipping duplicate job_url: %s", job_url)

    log.info("Inserted %d new candidates (searched %d)", len(inserted), len(jobs_df))
    return inserted


def _call_llm_score(prompt: str) -> dict[str, Any]:
    """Call configured LLM provider with prompt and return parsed JSON response."""
    from llm_provider import default_active_provider, provider_command

    provider = default_active_provider()
    cmd = provider_command(provider, prompt)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    output = result.stdout.strip()
    # Strip markdown code fences if present
    if output.startswith("```"):
        lines = output.splitlines()
        # Drop first line (```json or ```) and last closing ```
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        output = inner.strip()
    return json.loads(output)


def score_candidate(conn: sqlite3.Connection, candidate_id: int) -> dict[str, Any]:
    """Score a candidate job using Claude and update the DB row.

    Returns the updated candidate dict.
    """
    row = conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        msg = f"Candidate {candidate_id} not found"
        raise ValueError(msg)

    candidate = dict(row)

    # Load resume (truncated to 4000 chars)
    resume_text = ""
    if _RESUME_PATH.exists():
        resume_text = _RESUME_PATH.read_text(encoding="utf-8")[:4000]

    prompt = _SCORING_PROMPT.format(
        resume=resume_text,
        title=candidate.get("title") or "",
        company=candidate.get("company") or "",
        location=candidate.get("location") or "Unknown",
        salary=candidate.get("salary") or "Not specified",
        description=(candidate.get("job_description") or "")[:3000],
    )

    log.info("Scoring candidate %d: %s @ %s", candidate_id, candidate["title"], candidate["company"])

    try:
        response = _call_llm_score(prompt)
        score = int(response.get("score", 0))
        reason = str(response.get("reason", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to score candidate %d: %s", candidate_id, exc)
        score = 0
        reason = f"Scoring failed: {exc}"

    conn.execute(
        """
        UPDATE candidate_jobs
        SET score = ?, score_reason = ?, status = 'scored',
            scored_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (score, reason, candidate_id),
    )
    conn.commit()

    updated = dict(conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone())
    return updated


def score_unscored_candidates(conn: sqlite3.Connection) -> int:
    """Score all candidates with status='new' and score IS NULL.

    Returns count of candidates scored.
    """
    rows = conn.execute("SELECT id FROM candidate_jobs WHERE status = 'new' AND score IS NULL").fetchall()

    count = 0
    for row in rows:
        try:
            score_candidate(conn, row[0])
            count += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to score candidate %d: %s", row[0], exc)

    return count


def promote_candidate(conn: sqlite3.Connection, candidate_id: int) -> int | None:
    """Promote a candidate to the main jobs queue.

    Returns job_id if promoted, None if already promoted or duplicate URL.
    """
    from job_db import add_job

    row = conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        msg = f"Candidate {candidate_id} not found"
        raise ValueError(msg)

    candidate = dict(row)

    if candidate.get("promoted_job_id") is not None:
        log.info("Candidate %d already promoted as job %d", candidate_id, candidate["promoted_job_id"])
        return None

    # Use application_url if available, otherwise job_url.
    # Guard against "nan" strings from pandas NaN leaking through.
    app_url = candidate.get("application_url") or ""
    if app_url.lower() in ("", "nan", "none"):
        app_url = ""
    url = app_url or candidate["job_url"]

    try:
        job_id = add_job(
            conn,
            url,
            company=candidate.get("company"),
            role_title=candidate.get("title"),
            jd_text=candidate.get("job_description"),
        )
    except sqlite3.IntegrityError:
        log.info("Candidate %d URL already in jobs table: %s", candidate_id, url)
        return None

    if job_id < 0:
        existing_job_id = -job_id
        conn.execute(
            """
            UPDATE candidate_jobs
            SET promoted_job_id = ?, status = 'skipped',
                promoted_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (existing_job_id, candidate_id),
        )
        conn.commit()
        log.info("Skipped candidate %d as duplicate of job %d (%s)", candidate_id, existing_job_id, url)
        return None

    conn.execute(
        """
        UPDATE candidate_jobs
        SET promoted_job_id = ?, status = 'promoted',
            promoted_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (job_id, candidate_id),
    )
    conn.commit()

    log.info("Promoted candidate %d → job %d (%s)", candidate_id, job_id, url)
    return job_id


def list_candidates(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query candidates with optional filters, sorted by score DESC."""
    conditions: list[str] = []
    params: list[Any] = []

    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    if search is not None:
        conditions.append("(title LIKE ? OR company LIKE ? OR job_description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM candidate_jobs {where} ORDER BY score DESC LIMIT ? OFFSET ?",  # noqa: S608
        params,
    ).fetchall()

    return [dict(r) for r in rows]


def _normalize_company(name: str) -> str:
    """Normalize company name for fuzzy matching.

    Delegates to the shared normalization helper for consistency.
    """
    return normalize_company(name)


def _normalize_company_variants(name: str) -> set[str]:
    """Normalize company aliases for duplicate detection."""
    return company_match_variants(name)


def _normalize_title(title: str) -> set[str]:
    """Tokenize and normalize a role title for fuzzy matching."""
    import re

    title = title.lower()
    # Expand common abbreviations
    title = re.sub(r"\bsr\.?\b", "senior", title)
    title = re.sub(r"\bjr\.?\b", "junior", title)
    title = re.sub(r"\bpm\b", "product manager", title)
    title = re.sub(r"\btpm\b", "technical product manager", title)
    title = re.sub(r"\bswe\b", "software engineer", title)
    tokens = set(re.findall(r"[a-z]+", title))
    # Remove noise words
    tokens -= {"the", "a", "an", "and", "or", "of", "for", "in", "at", "to", "with"}
    return tokens


def find_duplicate_jobs(conn: sqlite3.Connection, candidates: list[dict]) -> dict[int, dict]:
    """Check candidates against existing jobs table for fuzzy company+title matches.

    Returns {candidate_id: {"job_id": N, "company": str, "role_title": str, "status": str}}
    for each candidate that has a likely duplicate in the jobs queue.
    """
    if not candidates:
        return {}

    # Load all non-archived jobs with company+title
    jobs = conn.execute(
        "SELECT id, company, role_title, status FROM jobs "
        "WHERE company IS NOT NULL AND role_title IS NOT NULL "
        "AND (archived IS NULL OR archived = 0)"
    ).fetchall()
    if not jobs:
        return {}

    # Build lookup: normalized_company_variant -> [(job_id, normalized_title, title_tokens, company, role_title, status)]
    job_index: dict[str, list] = {}
    for j in jobs:
        variants = _normalize_company_variants(j["company"])
        if not variants:
            continue
        job_entry = (
            j["id"],
            normalize_role_title(j["role_title"]),
            _normalize_title(j["role_title"]),
            j["company"],
            j["role_title"],
            j["status"],
        )
        for variant in variants:
            job_index.setdefault(variant, []).append(job_entry)

    duplicates: dict[int, dict] = {}
    for c in candidates:
        company_variants = _normalize_company_variants(c.get("company") or "")
        if not company_variants:
            continue
        c_tokens = _normalize_title(c.get("title") or "")
        normalized_candidate_title = normalize_role_title(c.get("title") or "")
        if not c_tokens:
            continue
        candidate_matches = []
        for variant in company_variants:
            candidate_matches.extend(job_index.get(variant, []))
        if not candidate_matches:
            continue
        seen_job_ids: set[int] = set()
        for job_id, normalized_job_title, j_tokens, j_company, j_role, j_status in candidate_matches:
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)
            # Jaccard similarity on title tokens
            intersection = c_tokens & j_tokens
            union = c_tokens | j_tokens
            titles_match_exactly = (
                normalized_candidate_title
                and normalized_job_title
                and normalized_candidate_title == normalized_job_title
            )
            if titles_match_exactly or (union and len(intersection) / len(union) >= 0.4):
                duplicates[c["id"]] = {
                    "job_id": job_id,
                    "company": j_company,
                    "role_title": j_role,
                    "status": j_status,
                }
                break  # One match is enough

    return duplicates


def skip_candidate(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Set candidate status to 'skipped'."""
    conn.execute(
        "UPDATE candidate_jobs SET status = 'skipped' WHERE id = ?",
        (candidate_id,),
    )
    conn.commit()


def delete_candidate(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Delete a candidate from the database."""
    conn.execute("DELETE FROM candidate_jobs WHERE id = ?", (candidate_id,))
    conn.commit()


def get_candidate_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Return dict of {status: count} for all candidates."""
    rows = conn.execute("SELECT status, COUNT(*) as cnt FROM candidate_jobs GROUP BY status").fetchall()
    return {row[0]: row[1] for row in rows}
