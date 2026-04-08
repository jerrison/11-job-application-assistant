#!/usr/bin/env python3
"""Runtime helpers for candidate-specific display defaults."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import materials_root
from application_models import parse_master_resume

DEFAULT_FULL_NAME = "Candidate Name"
DEFAULT_LOCATION = "San Francisco, CA"
DEFAULT_EMAIL = "candidate@example.com"
DEFAULT_PHONE = "555-0100"
DEFAULT_LINKEDIN = "linkedin.com/in/candidate/"
DEFAULT_WEBSITE = "candidate.example.com"


def _display_url(value: str | None, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


@dataclass(frozen=True, slots=True)
class CandidateRuntimeProfile:
    full_name: str
    full_name_upper: str
    email: str
    phone: str
    location: str
    linkedin: str
    website: str

    def contact_line(self, *, include_location: bool) -> str:
        parts: list[str] = []
        if include_location and self.location:
            parts.append(self.location)
        parts.extend(part for part in (self.email, self.phone, self.linkedin, self.website) if part)
        return "  |  ".join(parts)


def load_candidate_runtime_profile(master_resume_path: str | Path | None = None) -> CandidateRuntimeProfile:
    path = Path(master_resume_path) if master_resume_path is not None else materials_root() / "master_resume.md"
    if path.exists():
        try:
            profile = parse_master_resume(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            profile = None
        else:
            return CandidateRuntimeProfile(
                full_name=profile.full_name,
                full_name_upper=profile.full_name.upper(),
                email=profile.email or DEFAULT_EMAIL,
                phone=profile.phone or DEFAULT_PHONE,
                location=profile.location or DEFAULT_LOCATION,
                linkedin=_display_url(profile.linkedin, fallback=DEFAULT_LINKEDIN),
                website=_display_url(profile.website, fallback=DEFAULT_WEBSITE),
            )

    return CandidateRuntimeProfile(
        full_name=DEFAULT_FULL_NAME,
        full_name_upper=DEFAULT_FULL_NAME.upper(),
        email=DEFAULT_EMAIL,
        phone=DEFAULT_PHONE,
        location=DEFAULT_LOCATION,
        linkedin=DEFAULT_LINKEDIN,
        website=DEFAULT_WEBSITE,
    )


def document_filename(
    label: str,
    company_name: str,
    extension: str,
    *,
    master_resume_path: str | Path | None = None,
) -> str:
    profile = load_candidate_runtime_profile(master_resume_path)
    return f"{profile.full_name} {label} - {company_name}{extension}"
