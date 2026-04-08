"""Shared subprocess defaults for worker-style child processes."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from typing import Any


def prepare_worker_subprocess_kwargs(kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    run_kwargs = dict(kwargs or {})
    # Worker parents can have invalid or closed stdin after UI-triggered launches.
    run_kwargs.setdefault("stdin", subprocess.DEVNULL)
    return run_kwargs


def run_worker_subprocess(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), **prepare_worker_subprocess_kwargs(kwargs))


def popen_worker_subprocess(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(list(cmd), **prepare_worker_subprocess_kwargs(kwargs))
