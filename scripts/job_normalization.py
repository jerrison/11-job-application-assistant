"""Shared job-text normalization helpers."""

from __future__ import annotations

import hashlib
import re

_COMPANY_ALIAS_SUFFIXES = (
    " technologies",
    " technology",
    " tech",
    " labs",
    " lab",
)


def _collapsed_company_variant(normalized: str) -> str:
    return re.sub(r"\s+", "", normalized)


def normalize_company(name: str) -> str:
    """Normalize company name for fuzzy matching and fingerprinting."""
    name = name.lower().strip()
    for suffix in (
        ", inc.",
        ", inc",
        " inc.",
        " inc",
        ", llc",
        " llc",
        ", ltd",
        " ltd",
        " corp.",
        " corp",
        " co.",
        " co",
        ", l.p.",
        " l.p.",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = re.sub(r"[^a-z0-9 ]", "", name)
    return name.strip()


def company_match_variants(name: str) -> set[str]:
    """Return normalized company aliases for duplicate detection only."""
    normalized = normalize_company(name)
    if not normalized:
        return set()
    variants = {normalized}
    collapsed = _collapsed_company_variant(normalized)
    if collapsed and collapsed != normalized:
        variants.add(collapsed)
    for suffix in _COMPANY_ALIAS_SUFFIXES:
        if normalized.endswith(suffix):
            stripped = normalized[: -len(suffix)].strip()
            if stripped:
                variants.add(stripped)
                stripped_collapsed = _collapsed_company_variant(stripped)
                if stripped_collapsed != stripped:
                    variants.add(stripped_collapsed)
    return variants


def normalize_role_title(title: str) -> str:
    """Normalize role titles so wrapper text and PM synonyms dedupe cleanly."""
    normalized = title.casefold().strip()
    if not normalized:
        return ""
    normalized = re.sub(r"\|\s*linkedin\b.*$", "", normalized).strip()
    normalized = re.sub(r"^[a-z0-9&.'()/, -]+?\s+hiring\s+", "", normalized).strip()
    normalized = re.sub(r"\s+in\s+[a-z0-9&.'()/, -]+$", "", normalized).strip()
    normalized = re.sub(r"\bproduct manager\b", "pm", normalized)
    normalized = re.sub(r"\bproduct management\b", "pm", normalized)
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-")


def jd_fingerprint(company: str, jd: str | None) -> str | None:
    """Create a stable fingerprint from company + JD for cross-source dedup."""
    if not jd or len(jd) < 50:
        return None
    normalized_company = _collapsed_company_variant(normalize_company(company))
    normalized = normalized_company + "|" + re.sub(r"\s+", " ", jd[:2000].lower().strip())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]
