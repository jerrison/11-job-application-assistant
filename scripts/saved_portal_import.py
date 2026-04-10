from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from job_db import add_job, backfill_jd_fingerprints, find_jd_duplicates


class AuthRequiredError(RuntimeError):
    """Raised when saved-portal import cannot proceed without user auth."""


@dataclass(frozen=True, slots=True)
class SavedPortalSpec:
    key: str
    label: str
    module_name: str


_SAVED_PORTALS: tuple[SavedPortalSpec, ...] = (
    SavedPortalSpec(key="linkedin", label="LinkedIn", module_name="import_linkedin_saved"),
    SavedPortalSpec(key="trueup", label="TrueUp", module_name="import_trueup_saved"),
    SavedPortalSpec(key="jackandjill", label="Jack & Jill", module_name="import_jackandjill_saved"),
)


def list_saved_portals() -> list[SavedPortalSpec]:
    return list(_SAVED_PORTALS)


def get_saved_portal(portal: str) -> SavedPortalSpec:
    for spec in _SAVED_PORTALS:
        if spec.key == portal:
            return spec
    raise ValueError(f"Unknown saved portal: {portal}")


def load_saved_portal_module(portal: str):
    spec = get_saved_portal(portal)
    return importlib.import_module(spec.module_name)


def launch_saved_portal_auth_setup(portal: str) -> None:
    module = load_saved_portal_module(portal)
    launcher = getattr(module, "launch_auth_setup", None)
    if not callable(launcher):
        raise RuntimeError(f"Saved-portal auth setup is not supported: {portal}")
    launcher()


def _empty_result(status: str = "ok", message: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "scraped": 0,
        "resolved": 0,
        "added": 0,
        "duplicates": 0,
        "skipped_unresolved": 0,
        "errors": 0,
        "fingerprints_added": 0,
        "duplicate_groups": [],
        "samples": {"unresolved": [], "errors": []},
    }


def _append_sample(bucket: list[dict[str, Any]], item: dict[str, Any], limit: int = 5) -> None:
    if len(bucket) < limit:
        bucket.append(item)


def _source_url_for(candidate: Any, resolved: Mapping[str, Any] | None = None) -> str | None:
    if resolved:
        if resolved.get("source_url"):
            return str(resolved["source_url"])
    if isinstance(candidate, Mapping):
        if candidate.get("source_url"):
            return str(candidate["source_url"])
        if candidate.get("url"):
            return str(candidate["url"])
    if resolved and resolved.get("url"):
        return str(resolved["url"])
    return None


def import_saved_portal_jobs(
    conn: sqlite3.Connection,
    *,
    portal_name: str,
    scrape_jobs: Callable[[], list[Any]],
    resolve_job: Callable[[Any], Mapping[str, Any]],
    priority: int = 0,
    provider: str | None = None,
    on_duplicate: Callable[[sqlite3.Connection, Mapping[str, Any], int | None], None] | None = None,
) -> dict[str, Any]:
    result = _empty_result()

    try:
        candidates = scrape_jobs()
    except AuthRequiredError as exc:
        return _empty_result(status="auth_required", message=str(exc))

    result["scraped"] = len(candidates)

    for candidate in candidates:
        source_url = _source_url_for(candidate)
        try:
            resolved = resolve_job(candidate)
        except AuthRequiredError as exc:
            result["status"] = "auth_required"
            result["message"] = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            result["errors"] += 1
            _append_sample(
                result["samples"]["errors"],
                {"source_url": source_url, "reason": str(exc)},
            )
            continue

        source_url = _source_url_for(candidate, resolved)
        if resolved.get("status") == "unresolved":
            result["skipped_unresolved"] += 1
            _append_sample(
                result["samples"]["unresolved"],
                {
                    "source_url": source_url,
                    "reason": resolved.get("reason", "unresolved"),
                },
            )
            continue

        result["resolved"] += 1

        try:
            resolved_url = str(resolved["url"]).strip()
            existing_or_new_id = add_job(
                conn,
                resolved_url,
                priority=priority,
                provider=provider,
                company=resolved.get("company"),
                role_title=resolved.get("role_title"),
                jd_text=resolved.get("jd_text"),
                source_override=portal_name,
                source_url_override=source_url,
            )
        except sqlite3.IntegrityError:
            result["duplicates"] += 1
            if on_duplicate is not None:
                on_duplicate(conn, resolved, None)
            continue
        except Exception as exc:  # noqa: BLE001
            result["errors"] += 1
            _append_sample(
                result["samples"]["errors"],
                {"source_url": source_url, "reason": str(exc)},
            )
            continue

        if existing_or_new_id < 0:
            result["duplicates"] += 1
            if on_duplicate is not None:
                on_duplicate(conn, resolved, -existing_or_new_id)
            continue

        result["added"] += 1

    if result["added"] > 0:
        fingerprints_added, _fingerprints_skipped = backfill_jd_fingerprints(conn)
        result["fingerprints_added"] = fingerprints_added
        result["duplicate_groups"] = find_jd_duplicates(conn)

    return result
