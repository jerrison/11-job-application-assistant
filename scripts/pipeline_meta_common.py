#!/usr/bin/env python3
"""Shared helpers for loading and repairing saved pipeline metadata."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def load_pipeline_meta(output_dir: str | Path) -> dict:
    """Load .pipeline_meta.json and repair generic company metadata when possible."""
    output_dir = Path(output_dir)
    meta_path = output_dir / ".pipeline_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"{meta_path} does not contain an object")
    return normalize_pipeline_meta_company(output_dir, meta)


def load_pipeline_meta_if_present(output_dir: str | Path) -> dict | None:
    """Load .pipeline_meta.json when present, ignoring unreadable metadata."""
    output_dir = Path(output_dir)
    meta_path = output_dir / ".pipeline_meta.json"
    if not meta_path.is_file():
        return None
    try:
        return load_pipeline_meta(output_dir)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def enrich_pipeline_meta_urls(
    output_dir: str | Path,
    meta: dict,
    *,
    board_url: str,
    source_url: str,
    source: str,
) -> None:
    """Persist missing board/source URL metadata needed by later sync steps."""
    changed = False
    if board_url and not meta.get("board_url"):
        meta["board_url"] = board_url
        changed = True
    if source_url and source_url != board_url and not meta.get("source_url"):
        meta["source_url"] = source_url
        meta["source"] = source
        changed = True
    if changed:
        _persist_pipeline_meta(Path(output_dir), meta)


def normalize_pipeline_meta_company(output_dir: str | Path, meta: dict) -> dict:
    """Repair generic company metadata from saved JD content when possible."""
    from parse_jd import company_name_looks_generic, company_name_looks_locationish
    from run_pipeline import _company_name_from_text, _slugify

    output_dir = Path(output_dir)
    company_proper = str(meta.get("company_proper") or "").strip()
    company_slug = str(meta.get("company") or "").strip()
    jd_title = str(meta.get("jd_title") or "").strip()
    source_url = str(meta.get("jd_source") or meta.get("jd_source_resolved") or "")
    if (
        company_proper
        and not company_name_looks_generic(company_proper)
        and not company_name_looks_locationish(company_proper)
        and not _looks_like_keyword_placeholder_company(company_proper)
        and not _looks_like_loaded_page_title_wrapper(company_proper, jd_title)
        and not _is_source_wrapper_company(
            company_proper,
            source_url,
        )
    ):
        return meta

    for candidate_text in (str(meta.get("jd_title") or "").strip(),):
        candidate = _company_name_from_text(candidate_text) if candidate_text else None
        if candidate and not company_name_looks_generic(candidate):
            meta["company_proper"] = candidate
            if _company_slug_needs_repair(
                company_slug,
                source_url,
                company_name_looks_generic,
                company_name_looks_locationish,
                company_proper,
            ):
                meta["company"] = _slugify(candidate)
            _persist_pipeline_meta(output_dir, meta)
            return meta

    raw_paths = [output_dir / "content" / "jd_raw.md", output_dir / "jd_raw.md"]
    for raw_path in raw_paths:
        if not raw_path.is_file():
            continue
        try:
            raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        candidate = _company_name_from_text(raw_text)
        if candidate and not company_name_looks_generic(candidate):
            meta["company_proper"] = candidate
            if _company_slug_needs_repair(
                company_slug,
                source_url,
                company_name_looks_generic,
                company_name_looks_locationish,
                company_proper,
            ):
                meta["company"] = _slugify(candidate)
            _persist_pipeline_meta(output_dir, meta)
            return meta

    return meta


def _persist_pipeline_meta(output_dir: Path, meta: dict) -> None:
    meta_path = output_dir / ".pipeline_meta.json"
    try:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _is_source_wrapper_company(company_name: str, source_url: str | None) -> bool:
    normalized = _normalize_company_name(company_name)
    if not normalized:
        return False

    host = (urlparse(str(source_url or "")).hostname or "").removeprefix("www.").casefold()
    if not host:
        return False

    wrapper_names: set[str] = set()
    if host.endswith("linkedin.com"):
        wrapper_names.add("linkedin")
    if "greenhouse.io" in host:
        wrapper_names.add("greenhouse")
    if host.endswith("indeed.com"):
        wrapper_names.add("indeed")
    if "glassdoor" in host:
        wrapper_names.add("glassdoor")
    if host.endswith("lever.co"):
        wrapper_names.add("lever")
    if host.endswith("ashbyhq.com"):
        wrapper_names.add("ashby")
    if host == "jobs.gem.com":
        wrapper_names.add("gem")

    return normalized in wrapper_names


def _company_slug_needs_repair(
    company_slug: str,
    source_url: str,
    company_name_looks_generic,
    company_name_looks_locationish,
    company_proper: str,
) -> bool:
    normalized = company_slug.strip()
    return (
        not normalized
        or company_name_looks_generic(normalized)
        or company_name_looks_locationish(normalized)
        or _looks_like_keyword_placeholder_company(normalized)
        or _looks_like_keyword_placeholder_company(company_proper)
        or _is_source_wrapper_company(normalized, source_url)
    )


def _normalize_company_name(value: str | None) -> str:
    return " ".join("".join(char if char.isalnum() else " " for char in str(value or "").casefold()).split())


def _looks_like_loaded_page_title_wrapper(company_name: str, jd_title: str | None) -> bool:
    normalized_company = _normalize_company_name(company_name)
    normalized_title = _normalize_company_name(jd_title)
    if not normalized_company or not normalized_title:
        return False
    return normalized_company == f"{normalized_title} page"


def _looks_like_keyword_placeholder_company(company_name: str | None) -> bool:
    normalized = _normalize_company_name(company_name)
    if not normalized:
        return False
    return normalized in {
        "ai",
        "ai ml",
        "gen ai",
        "generative ai",
        "llm",
        "machine learning",
        "ml",
    }
