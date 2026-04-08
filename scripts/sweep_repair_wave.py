#!/usr/bin/env python3
"""Repair-wave fingerprint helpers used by sweep tooling."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

PathLike = str | Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOBS = (
    "scripts/**/*.py",
    "scripts/static/**",
    "AGENTS.md",
    "docs/operational-rules.md",
    "docs/backlog-sweep.md",
    "docs/runbooks/repeatable-backlog-sweep.md",
)


def iter_repair_wave_paths(project_root: Path = PROJECT_ROOT) -> list[Path]:
    resolved_paths: set[Path] = set()
    for pattern in DEFAULT_GLOBS:
        for path in project_root.glob(pattern):
            if not path.is_file():
                continue
            resolved_paths.add(path.resolve())
    return sorted(resolved_paths)


def _coerce_path(value: PathLike) -> Path:
    if isinstance(value, Path):
        return value
    return Path(value)


def _hash_identifier(path: Path) -> bytes:
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError:
        identifier = path.as_posix()
    else:
        identifier = relative.as_posix()
    return identifier.encode("utf-8")


def compute_repair_wave_fingerprint(
    paths: Iterable[PathLike],
    *,
    ignored_paths: Iterable[PathLike] = (),
) -> str:
    ignored = {_coerce_path(path).resolve() for path in ignored_paths}
    hasher = hashlib.sha256()
    ordered = sorted({_coerce_path(path).resolve() for path in paths})
    for path in ordered:
        if path in ignored or not path.is_file():
            continue
        data = path.read_bytes()
        hasher.update(_hash_identifier(path))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")
    return hasher.hexdigest()


def current_repair_wave_fingerprint(project_root: Path = PROJECT_ROOT) -> str:
    return compute_repair_wave_fingerprint(iter_repair_wave_paths(project_root))
