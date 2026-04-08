#!/usr/bin/env python3
"""Persist user-editable materials and provider configuration."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import env_file_paths, materials_root
from llm_provider import automation_provider_chain, default_active_provider, effective_provider_settings
from project_env import ENV_LINE_RE, parse_env_file

MATERIAL_FILES = {
    "master_resume": "master_resume.md",
    "work_stories": "work_stories.md",
    "candidate_context": "candidate_context.md",
    "application_profile": "application_profile.md",
}
PROVIDER_ENV_FIELDS = {
    "default_provider": "ASSET_LLM_PROVIDER",
    "provider_chain": "ASSET_LLM_PROVIDER_CHAIN",
    "openai_model": "OPENAI_MODEL",
    "gemini_model": "GEMINI_MODEL",
    "gemini_flash_model": "GEMINI_FLASH_MODEL",
    "codex_model": "CODEX_MODEL",
    "claude_model": "CLAUDE_MODEL",
    "steel_base_url": "STEEL_BASE_URL",
}
BOOL_PROVIDER_ENV_FIELDS = {
    "steel_local": "STEEL_LOCAL",
}
SECRET_ENV_FIELDS = {
    "openai_api_key": "OPENAI_API_KEY",
    "openai_api_keys": "OPENAI_API_KEYS",
    "gemini_api_key": "GEMINI_API_KEY",
    "codex_api_key": "CODEX_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "steel_api_key": "STEEL_API_KEY",
}
MANAGED_ENV_KEYS = {*PROVIDER_ENV_FIELDS.values(), *BOOL_PROVIDER_ENV_FIELDS.values(), *SECRET_ENV_FIELDS.values()}
ONBOARDING_REQUIRED_MATERIALS = ("master_resume",)
ONBOARDING_PRIMARY_CREDENTIALS = (
    "openai_api_key",
    "openai_api_keys",
    "gemini_api_key",
    "codex_api_key",
    "anthropic_api_key",
)


def _material_path(key: str, *, environ: Mapping[str, str] | None = None) -> Path:
    filename = MATERIAL_FILES.get(key)
    if filename is None:
        raise ValueError(f"Unsupported material key: {key}")
    return materials_root(environ=dict(environ) if environ is not None else None) / filename


def _env_local_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return env_file_paths(environ=dict(environ) if environ is not None else None)[-1]


def _resolved_env(*, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for path in env_file_paths(environ=dict(environ) if environ is not None else None):
        resolved.update(parse_env_file(path))
    if environ is None:
        resolved.update(os.environ)
    else:
        resolved.update(environ)
    return resolved


def _bool_value(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _secret_preview(value: str) -> str:
    entries = [entry.strip() for entry in value.replace(",", "\n").splitlines() if entry.strip()]
    if len(entries) > 1:
        return f"{len(entries)} configured"
    normalized = entries[0] if entries else value.strip()
    if len(normalized) <= 8:
        return "*" * len(normalized)
    return f"{normalized[:4]}...{normalized[-4:]}"


def _serialize_env_value(value: str) -> str:
    return json.dumps(value)


def _upsert_env_local(updates: Mapping[str, str | None], *, environ: Mapping[str, str] | None = None) -> None:
    path = _env_local_path(environ=environ)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    rendered: list[str] = []

    for raw_line in existing_lines:
        stripped = raw_line.strip()
        match = ENV_LINE_RE.match(stripped) if stripped and not stripped.startswith("#") else None
        if not match:
            rendered.append(raw_line)
            continue

        key = match.group(1)
        if key not in pending:
            rendered.append(raw_line)
            continue

        value = pending.pop(key)
        if value is None:
            continue
        rendered.append(f"{key}={_serialize_env_value(value)}")

    for key, value in pending.items():
        if value is None:
            continue
        rendered.append(f"{key}={_serialize_env_value(value)}")

    content = "\n".join(rendered).rstrip()
    path.write_text(f"{content}\n" if content else "", encoding="utf-8")


def _apply_env_updates(target: MutableMapping[str, str], updates: Mapping[str, str | None]) -> None:
    for key, value in updates.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


def _material_metadata(*, environ: Mapping[str, str] | None = None) -> dict[str, dict[str, object]]:
    materials: dict[str, dict[str, object]] = {}
    for key in MATERIAL_FILES:
        path = _material_path(key, environ=environ)
        exists = path.exists()
        materials[key] = {
            "path": str(path),
            "exists": exists,
            "has_content": exists and path.stat().st_size > 0,
        }
    return materials


def _credential_metadata(resolved_env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    credentials: dict[str, dict[str, object]] = {}
    for key, env_key in SECRET_ENV_FIELDS.items():
        raw = resolved_env.get(env_key, "").strip()
        credentials[key] = {
            "configured": bool(raw),
            "preview": _secret_preview(raw) if raw else "",
        }
    return credentials


def load_bootstrap(*, environ: Mapping[str, str] | None = None) -> dict:
    resolved_env = _resolved_env(environ=environ)
    materials = _material_metadata(environ=environ)
    credentials = _credential_metadata(resolved_env)

    required_materials = {
        key: bool(materials[key]["has_content"]) for key in ONBOARDING_REQUIRED_MATERIALS
    }
    recommended_materials = {
        key: bool(entry["has_content"]) for key, entry in materials.items() if key not in ONBOARDING_REQUIRED_MATERIALS
    }
    credentials_ready = any(credentials[key]["configured"] for key in ONBOARDING_PRIMARY_CREDENTIALS)

    return {
        "materials": materials,
        "providers": {
            "default_provider": default_active_provider(environ=resolved_env),
        },
        "credentials": {
            key: {"configured": bool(meta["configured"])} for key, meta in credentials.items()
        },
        "onboarding": {
            "complete": all(required_materials.values()) and credentials_ready,
            "required_materials": required_materials,
            "recommended_materials": recommended_materials,
            "credentials_ready": credentials_ready,
        },
    }


def load_settings(*, environ: Mapping[str, str] | None = None) -> dict:
    resolved_env = _resolved_env(environ=environ)
    materials = _material_metadata(environ=environ)
    for _key, entry in materials.items():
        path = Path(str(entry["path"]))
        entry["content"] = path.read_text(encoding="utf-8") if entry["exists"] else ""

    providers = {
        "default_provider": default_active_provider(environ=resolved_env),
        "provider_chain": ",".join(automation_provider_chain(environ=resolved_env)),
        "openai_model": effective_provider_settings("openai", environ=resolved_env)["model"],
        "gemini_model": effective_provider_settings("gemini", environ=resolved_env)["model"],
        "gemini_flash_model": effective_provider_settings("gemini-flash", environ=resolved_env)["model"],
        "codex_model": effective_provider_settings("codex", environ=resolved_env)["model"],
        "claude_model": effective_provider_settings("claude", environ=resolved_env)["model"],
        "steel_local": _bool_value(resolved_env.get("STEEL_LOCAL")),
        "steel_base_url": resolved_env.get("STEEL_BASE_URL", "").strip(),
    }

    credentials = _credential_metadata(resolved_env)

    return {
        "materials": materials,
        "providers": providers,
        "credentials": credentials,
    }


def save_settings(payload: Mapping[str, object], *, environ: MutableMapping[str, str] | None = None) -> dict:
    target_env = environ if environ is not None else os.environ

    materials_payload = payload.get("materials") or {}
    if not isinstance(materials_payload, Mapping):
        raise ValueError("materials must be an object")
    for key, value in materials_payload.items():
        if key not in MATERIAL_FILES:
            raise ValueError(f"Unsupported material key: {key}")
        if not isinstance(value, str):
            raise ValueError(f"Material {key} must be a string")
        path = _material_path(key, environ=target_env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    env_updates: dict[str, str | None] = {}

    providers_payload = payload.get("providers") or {}
    if not isinstance(providers_payload, Mapping):
        raise ValueError("providers must be an object")
    for key, value in providers_payload.items():
        if key in PROVIDER_ENV_FIELDS:
            env_key = PROVIDER_ENV_FIELDS[key]
            if value is None:
                env_updates[env_key] = None
            else:
                env_updates[env_key] = str(value).strip() or None
            continue
        if key in BOOL_PROVIDER_ENV_FIELDS:
            env_key = BOOL_PROVIDER_ENV_FIELDS[key]
            env_updates[env_key] = "true" if bool(value) else "false"
            continue
        raise ValueError(f"Unsupported provider setting: {key}")

    credentials_payload = payload.get("credentials") or {}
    if not isinstance(credentials_payload, Mapping):
        raise ValueError("credentials must be an object")
    for key, value in credentials_payload.items():
        env_key = SECRET_ENV_FIELDS.get(key)
        if env_key is None:
            raise ValueError(f"Unsupported credential setting: {key}")
        if value is None:
            env_updates[env_key] = None
            continue
        normalized = str(value).strip()
        env_updates[env_key] = normalized or None

    managed_updates = {key: value for key, value in env_updates.items() if key in MANAGED_ENV_KEYS}
    if managed_updates:
        _upsert_env_local(managed_updates, environ=target_env)
        _apply_env_updates(target_env, managed_updates)

    return load_settings(environ=target_env)
