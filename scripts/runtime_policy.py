#!/usr/bin/env python3
"""Policy-as-code runtime checks for shared operator surfaces."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import code_root
from runtime_trace import emit_trace

POLICY_PATH_ENV = "JOB_ASSETS_POLICY_PATH"
VALID_POLICY_TIERS = frozenset({"L0", "L1", "L2", "L3"})


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    action: str
    tier: str
    allow: bool
    requires_explicit_approval: bool
    required_metadata: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: str
    tier: str
    allowed: bool
    requires_explicit_approval: bool
    reason: str
    policy_version: int


def policy_path(*, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = str(env.get(POLICY_PATH_ENV, "")).strip()
    if explicit:
        return Path(explicit).expanduser()
    return code_root(environ=dict(env)) / "governance" / "runtime-policy.json"


def _normalize_action_policy(action: str, raw_policy: Mapping[str, object] | None) -> ActionPolicy:
    policy = dict(raw_policy or {})
    tier = str(policy.get("tier") or "L0").strip().upper()
    if tier not in VALID_POLICY_TIERS:
        raise ValueError(f"Invalid policy tier for {action}: {tier}")
    allow = bool(policy.get("allow", True))
    requires_explicit_approval = bool(policy.get("requires_explicit_approval"))
    required_metadata = policy.get("required_metadata") or []
    if not isinstance(required_metadata, list) or any(not str(item).strip() for item in required_metadata):
        raise ValueError(f"Invalid required_metadata for {action}")
    return ActionPolicy(
        action=action,
        tier=tier,
        allow=allow,
        requires_explicit_approval=requires_explicit_approval,
        required_metadata=tuple(str(item).strip() for item in required_metadata),
    )


def load_runtime_policy(*, environ: Mapping[str, str] | None = None) -> dict:
    path = policy_path(environ=environ)
    if not path.exists():
        return {"version": 1, "actions": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Runtime policy must be a JSON object")
    version = int(payload.get("version") or 1)
    actions_payload = payload.get("actions") or {}
    if not isinstance(actions_payload, dict):
        raise ValueError("Runtime policy actions must be a JSON object")
    normalized_actions = {}
    for action, raw_policy in actions_payload.items():
        normalized = _normalize_action_policy(str(action), raw_policy)
        normalized_actions[str(action)] = {
            "tier": normalized.tier,
            "allow": normalized.allow,
            "requires_explicit_approval": normalized.requires_explicit_approval,
            "required_metadata": list(normalized.required_metadata),
        }
    return {"version": version, "actions": normalized_actions}


def _metadata_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def evaluate_action(
    action: str,
    *,
    explicit_approval: bool = False,
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> PolicyDecision:
    policy = load_runtime_policy(environ=environ)
    policy_version = int(policy.get("version") or 1)
    actions = policy.get("actions") or {}
    action_policy = _normalize_action_policy(action, actions.get(action))
    metadata_dict = dict(metadata or {})
    missing_metadata = sorted(key for key in action_policy.required_metadata if not _metadata_present(metadata_dict.get(key)))
    if missing_metadata:
        allowed = False
        reason = f"{action} missing required metadata: {', '.join(missing_metadata)}"
    elif not action_policy.allow:
        allowed = False
        reason = f"{action} is disabled by policy."
    elif action_policy.requires_explicit_approval and not explicit_approval:
        allowed = False
        reason = f"{action} requires explicit approval."
    else:
        allowed = True
        reason = "allowed"
    decision = PolicyDecision(
        action=action,
        tier=action_policy.tier,
        allowed=allowed,
        requires_explicit_approval=action_policy.requires_explicit_approval,
        reason=reason,
        policy_version=policy_version,
    )
    emit_trace(
        "policy_decision",
        action=action,
        status="allowed" if allowed else "blocked",
        metadata={
            "tier": action_policy.tier,
            "policy_version": policy_version,
            "requires_explicit_approval": action_policy.requires_explicit_approval,
            "required_metadata": list(action_policy.required_metadata),
            "missing_metadata": missing_metadata,
            "reason": reason,
            **metadata_dict,
        },
        environ=environ,
    )
    return decision


def ensure_action_allowed(
    action: str,
    *,
    explicit_approval: bool = False,
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> PolicyDecision:
    decision = evaluate_action(
        action,
        explicit_approval=explicit_approval,
        metadata=metadata,
        environ=environ,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
    return decision
