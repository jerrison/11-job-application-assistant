#!/usr/bin/env python3
"""Shared guardrails for repo entrypoints launched from provider subtasks."""

from __future__ import annotations

import os

FORBID_RECURSIVE_ENTRYPOINTS_ENV = "JOB_ASSETS_FORBID_RECURSIVE_ENTRYPOINTS"


def recursive_entrypoints_forbidden(*, environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    raw = str(env.get(FORBID_RECURSIVE_ENTRYPOINTS_ENV, "")).strip()
    return raw not in {"", "0", "false", "False", "FALSE"}


def abort_if_recursive_entrypoints_forbidden(entrypoint: str) -> None:
    if not recursive_entrypoints_forbidden():
        return
    raise SystemExit(
        f"ERROR: {entrypoint} cannot be invoked from inside a non-interactive provider subtask. "
        "Read the prepared inputs and write the requested output files directly instead of recursing "
        "into repo entrypoints."
    )
