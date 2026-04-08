"""Helpers for structured audit metadata on user-triggered job actions."""

from __future__ import annotations

from collections.abc import Mapping


def build_action_detail_json(
    *,
    surface: str | None = None,
    trigger: str | None = None,
    route: str | None = None,
    request_id: str | None = None,
) -> dict | None:
    action: dict[str, str] = {}
    for key, value in (
        ("surface", surface),
        ("trigger", trigger),
        ("route", route),
        ("request_id", request_id),
    ):
        cleaned = str(value or "").strip()
        if cleaned:
            action[key] = cleaned
    return {"action": action} if action else None


def build_action_process_info(detail_json: dict | None) -> str | None:
    if not isinstance(detail_json, dict):
        return None
    action = detail_json.get("action")
    if not isinstance(action, dict):
        return None

    parts: list[str] = []
    surface = str(action.get("surface") or "").strip()
    if surface:
        parts.append(f"action_surface={surface}")
    trigger = str(action.get("trigger") or "").strip()
    if trigger:
        parts.append(f"action_trigger={trigger}")
    request_id = str(action.get("request_id") or "").strip()
    if request_id:
        parts.append(f"request_id={request_id}")
    route = str(action.get("route") or "").strip()
    if route:
        parts.append(f"route={route}")
    return " ".join(parts) or None


def extract_action_detail_json_from_headers(
    headers: Mapping[str, str],
    *,
    route: str,
    default_surface: str = "api",
    default_trigger: str = "api",
) -> dict | None:
    return build_action_detail_json(
        surface=headers.get("X-Jobapps-Action-Surface") or default_surface,
        trigger=headers.get("X-Jobapps-Action-Trigger") or default_trigger,
        request_id=headers.get("X-Jobapps-Request-Id"),
        route=route,
    )
