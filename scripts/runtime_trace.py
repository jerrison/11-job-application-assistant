#!/usr/bin/env python3
"""Redacted runtime tracing for shared governance and audit visibility."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import traces_root

TRACE_DISABLED_ENV = "JOB_ASSETS_TRACE_DISABLED"
TRACE_PATH_ENV = "JOB_ASSETS_TRACE_PATH"
PACKAGED_RUNTIME_ENV = "JOB_ASSETS_PACKAGED"
_SECRET_KEY_FRAGMENTS = ("api_key", "token", "secret", "password", "authorization", "cookie")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def trace_path(*, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = str(env.get(TRACE_PATH_ENV, "")).strip()
    if explicit:
        return Path(explicit).expanduser()
    return traces_root(environ=dict(env)) / "runtime-trace.jsonl"


def _mask_secret(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "[redacted]"
    if len(normalized) <= 8:
        return "[redacted]"
    return f"{normalized[:4]}...{normalized[-4:]}"


def _redact_string(value: str) -> str:
    masked = _OPENAI_KEY_RE.sub(lambda match: _mask_secret(match.group(0)), value)
    masked = _EMAIL_RE.sub("[redacted-email]", masked)
    return masked


def _redact_value(value: object, *, key: str | None = None) -> object:
    lowered_key = str(key or "").casefold()
    if any(fragment in lowered_key for fragment in _SECRET_KEY_FRAGMENTS):
        return _mask_secret(value)
    if isinstance(value, Mapping):
        return {str(child_key): _redact_value(child_value, key=str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


class TraceProcessor(Protocol):
    def process_event(self, event: Mapping[str, object]) -> None: ...


class JsonlTraceProcessor:
    def __init__(self, path: Path):
        self._path = path

    def process_event(self, event: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), sort_keys=True) + "\n")


class RedactingTraceProcessor:
    def __init__(self, downstream: TraceProcessor):
        self._downstream = downstream

    def process_event(self, event: Mapping[str, object]) -> None:
        self._downstream.process_event(_redact_value(dict(event)))


_TRACE_PROCESSOR_LOCK = threading.Lock()
_REGISTERED_TRACE_PROCESSORS: list[TraceProcessor] = []


def build_default_trace_processors(*, environ: Mapping[str, str] | None = None) -> tuple[TraceProcessor, ...]:
    return (RedactingTraceProcessor(JsonlTraceProcessor(trace_path(environ=environ))),)


def trace_processors() -> tuple[TraceProcessor, ...]:
    with _TRACE_PROCESSOR_LOCK:
        return tuple(_REGISTERED_TRACE_PROCESSORS)


def clear_trace_processors() -> None:
    with _TRACE_PROCESSOR_LOCK:
        _REGISTERED_TRACE_PROCESSORS.clear()


def configure_runtime_trace(
    *,
    environ: Mapping[str, str] | None = None,
    processors: Sequence[TraceProcessor] | None = None,
    replace: bool = True,
) -> tuple[TraceProcessor, ...]:
    configured = tuple(processors or build_default_trace_processors(environ=environ))
    with _TRACE_PROCESSOR_LOCK:
        if replace:
            _REGISTERED_TRACE_PROCESSORS[:] = list(configured)
        else:
            _REGISTERED_TRACE_PROCESSORS.extend(configured)
        return tuple(_REGISTERED_TRACE_PROCESSORS)


def default_trace_processors(*, environ: Mapping[str, str] | None = None) -> tuple[TraceProcessor, ...]:
    configured = trace_processors()
    if configured:
        return configured
    return build_default_trace_processors(environ=environ)


def emit_trace(
    event_type: str,
    *,
    action: str,
    status: str = "ok",
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
    processors: Sequence[TraceProcessor] | None = None,
) -> dict[str, object]:
    env = environ if environ is not None else os.environ
    event = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "event_type": str(event_type),
        "action": str(action),
        "status": str(status),
        "metadata": dict(metadata or {}),
        "runtime": {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "packaged": _truthy(env.get(PACKAGED_RUNTIME_ENV)) or bool(getattr(sys, "frozen", False)),
        },
    }
    if _truthy(env.get(TRACE_DISABLED_ENV)):
        return event
    for processor in processors or default_trace_processors(environ=env):
        processor.process_event(event)
    return event
