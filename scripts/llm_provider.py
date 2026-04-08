#!/usr/bin/env python3
"""Shared provider defaults and command builders for non-interactive LLM calls."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from runtime_entrypoints import python_script_command

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CODEX_EXEC_WRAPPER = PROJECT_ROOT / "scripts" / "codex_exec_wrapper.py"
OPENAI_PROVIDER_SCRIPT = PROJECT_ROOT / "scripts" / "openai_provider.py"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CLAUDE_EFFORT = "max"
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"
DEFAULT_CLAUDE_SETTING_SOURCES = "project,local"
DEFAULT_CLAUDE_MCP_CONFIG = '{"mcpServers":{}}'
DEFAULT_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS = 600
DEFAULT_CLAUDE_ASSET_FALLBACK_PROVIDER = ""
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_CODEX_REASONING_EFFORT = "xhigh"
DEFAULT_CODEX_APPROVAL_POLICY = "never"
DEFAULT_CODEX_SANDBOX_MODE = "danger-full-access"
DEFAULT_ACTIVE_PROVIDER = "openai"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_GEMINI_FLASH_MODEL = DEFAULT_GEMINI_MODEL
DEFAULT_OPENAI_MODEL = "gpt-5.4"
VALID_OPENAI_REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh")
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600
DEFAULT_ASSET_PROVIDER_TIMEOUT_SECONDS = 1200
AUTOMATION_PROVIDERS: tuple[str, ...] = ("openai", "gemini")

# Canonical set of valid LLM provider names accepted by all CLI entrypoints.
# "gemini-flash" uses the same ``gemini`` binary but targets the flash model.
VALID_PROVIDERS: tuple[str, ...] = ("gemini", "gemini-flash", "claude", "codex", "openai")
CLAUDE_RESEARCH_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Bash(uv run:*),Bash(curl:*)"
CLAUDE_DRAFT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"
CLAUDE_FIX_ALLOWED_TOOLS = "Read,Write,Edit"
CLAUDE_SUBMIT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"


def provider_binary(provider: str) -> str:
    """Return the CLI binary name for a provider (handles variants like gemini-flash)."""
    if provider == "gemini-flash":
        return "gemini"
    if provider == "openai":
        return sys.executable
    return provider


def provider_available(provider: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the configured provider looks runnable in this environment."""
    env = environ if environ is not None else os.environ
    if shutil.which(provider_binary(provider)) is None:
        return False
    if provider != "openai":
        return True
    if not _configured_openai_api_keys(environ=env):
        return False
    return importlib.util.find_spec("openai") is not None


def default_active_provider(*, environ: Mapping[str, str] | None = None) -> str:
    """Return the configured primary provider, falling back to the project default."""
    env = environ if environ is not None else os.environ
    return _clean(env.get("ASSET_LLM_PROVIDER")) or DEFAULT_ACTIVE_PROVIDER


def default_provider_chain(*, environ: Mapping[str, str] | None = None) -> str:
    """Return the configured provider chain, falling back to the active provider."""
    env = environ if environ is not None else os.environ
    return _clean(env.get("ASSET_LLM_PROVIDER_CHAIN")) or default_active_provider(environ=env)


def automation_provider_chain(*, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the automation fallback chain restricted to OpenAI and Gemini.

    Automation paths should only use OpenAI and Gemini even if older env
    values still mention Claude or Codex. When the configured chain only names
    one allowed provider, append the other so automated retries can exhaust the
    intended pair before giving up.
    """
    env = environ if environ is not None else os.environ
    configured = [
        provider for provider in _split_env_list(env.get("ASSET_LLM_PROVIDER_CHAIN")) if provider in AUTOMATION_PROVIDERS
    ]
    if configured:
        chain = list(configured)
    else:
        active = default_active_provider(environ=env)
        chain = [active] if active in AUTOMATION_PROVIDERS else [DEFAULT_ACTIVE_PROVIDER]
    for provider in AUTOMATION_PROVIDERS:
        if provider not in chain:
            chain.append(provider)
    return tuple(chain)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _split_extra_args(value: str | None) -> list[str]:
    cleaned = _clean(value)
    if cleaned is None:
        return []
    try:
        return shlex.split(cleaned)
    except ValueError:
        return [cleaned]


def _split_env_list(value: str | None) -> list[str]:
    cleaned = _clean(value)
    if cleaned is None:
        return []

    values: list[str] = []
    seen: set[str] = set()
    normalized = cleaned.replace(",", "\n")
    for raw_item in normalized.splitlines():
        item = raw_item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        values.append(item)
    return values


def _configured_openai_api_keys(*, environ: Mapping[str, str] | None = None) -> list[str]:
    env = environ if environ is not None else os.environ
    pooled_keys = _split_env_list(env.get("OPENAI_API_KEYS"))
    if pooled_keys:
        return pooled_keys
    api_key = _clean(env.get("OPENAI_API_KEY"))
    return [api_key] if api_key is not None else []


def _validated_openai_reasoning_effort(value: str | None) -> str:
    cleaned = _clean(value) or ""
    if not cleaned:
        return ""
    if cleaned not in VALID_OPENAI_REASONING_EFFORTS:
        supported = ", ".join(VALID_OPENAI_REASONING_EFFORTS)
        raise ValueError(
            f"Unsupported OpenAI reasoning effort: {cleaned}. Supported values: {supported}"
        )
    return cleaned


def _load_toml(path: Path) -> dict:
    if tomllib is None or not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _runtime_codex_defaults(*, environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is not None and "JOB_ASSETS_CODEX_CONFIG_PATH" not in environ:
        return {}

    if environ is None:
        config_path = Path.home() / ".codex" / "config.toml"
    else:
        raw_path = _clean(environ.get("JOB_ASSETS_CODEX_CONFIG_PATH"))
        if raw_path is None:
            return {}
        config_path = Path(raw_path).expanduser()

    data = _load_toml(config_path)
    resolved: dict[str, str] = {}
    for source_key, target_key in (
        ("model", "model"),
        ("model_reasoning_effort", "reasoning_effort"),
        ("approval_policy", "approval_policy"),
        ("sandbox_mode", "sandbox_mode"),
    ):
        value = data.get(source_key)
        if isinstance(value, str) and value.strip():
            resolved[target_key] = value.strip()
    return resolved


def effective_provider_settings(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = environ if environ is not None else os.environ
    if provider == "claude":
        return {
            "model": _clean(env.get("CLAUDE_MODEL")) or DEFAULT_CLAUDE_MODEL,
            "effort": _clean(env.get("CLAUDE_EFFORT")) or DEFAULT_CLAUDE_EFFORT,
            "permission_mode": _clean(env.get("CLAUDE_PERMISSION_MODE")) or DEFAULT_CLAUDE_PERMISSION_MODE,
            "setting_sources": _clean(env.get("CLAUDE_SETTING_SOURCES")) or DEFAULT_CLAUDE_SETTING_SOURCES,
            "no_session_persistence": _clean(env.get("CLAUDE_NO_SESSION_PERSISTENCE")) or "1",
            "disable_slash_commands": _clean(env.get("CLAUDE_DISABLE_SLASH_COMMANDS")) or "1",
            "strict_mcp_config": _clean(env.get("CLAUDE_STRICT_MCP_CONFIG")) or "1",
            "mcp_config": _clean(env.get("CLAUDE_MCP_CONFIG")) or DEFAULT_CLAUDE_MCP_CONFIG,
            "extra_args": _clean(env.get("CLAUDE_EXTRA_ARGS")) or "",
            "asset_primary_timeout_seconds": str(claude_primary_asset_timeout_seconds(environ=env)),
            "asset_fallback_provider": claude_asset_fallback_provider(environ=env),
            "profile": "",
            "reasoning_effort": "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }

    if provider == "codex":
        runtime_defaults = _runtime_codex_defaults(environ=environ)
        return {
            "model": _clean(env.get("CODEX_MODEL")) or runtime_defaults.get("model") or DEFAULT_CODEX_MODEL,
            "effort": "",
            "asset_primary_timeout_seconds": "",
            "asset_fallback_provider": "",
            "profile": _clean(env.get("CODEX_PROFILE")) or "",
            "reasoning_effort": _clean(env.get("CODEX_REASONING_EFFORT"))
            or runtime_defaults.get("reasoning_effort")
            or DEFAULT_CODEX_REASONING_EFFORT,
            "approval_policy": _clean(env.get("CODEX_APPROVAL_POLICY"))
            or runtime_defaults.get("approval_policy")
            or DEFAULT_CODEX_APPROVAL_POLICY,
            "sandbox_mode": _clean(env.get("CODEX_SANDBOX_MODE"))
            or runtime_defaults.get("sandbox_mode")
            or DEFAULT_CODEX_SANDBOX_MODE,
            "extra_args": _clean(env.get("CODEX_EXTRA_ARGS")) or "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }

    if provider == "gemini-flash":
        return {
            "model": _clean(env.get("GEMINI_FLASH_MODEL")) or DEFAULT_GEMINI_FLASH_MODEL,
            "effort": "",
            "permission_mode": "",
            "setting_sources": "",
            "no_session_persistence": "",
            "disable_slash_commands": "",
            "strict_mcp_config": "",
            "mcp_config": "",
            "asset_primary_timeout_seconds": "",
            "asset_fallback_provider": "",
            "profile": "",
            "reasoning_effort": "",
            "approval_policy": "",
            "sandbox_mode": "",
            "extra_args": _clean(env.get("GEMINI_EXTRA_ARGS")) or "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }

    if provider == "gemini":
        return {
            "model": _clean(env.get("GEMINI_MODEL")) or DEFAULT_GEMINI_MODEL,
            "effort": "",
            "permission_mode": "",
            "setting_sources": "",
            "no_session_persistence": "",
            "disable_slash_commands": "",
            "strict_mcp_config": "",
            "mcp_config": "",
            "asset_primary_timeout_seconds": "",
            "asset_fallback_provider": "",
            "profile": "",
            "reasoning_effort": "",
            "approval_policy": "",
            "sandbox_mode": "",
            "extra_args": _clean(env.get("GEMINI_EXTRA_ARGS")) or "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }

    if provider == "openai":
        return {
            "model": _clean(env.get("OPENAI_MODEL")) or DEFAULT_OPENAI_MODEL,
            "effort": "",
            "permission_mode": "",
            "setting_sources": "",
            "no_session_persistence": "",
            "disable_slash_commands": "",
            "strict_mcp_config": "",
            "mcp_config": "",
            "asset_primary_timeout_seconds": "",
            "asset_fallback_provider": "",
            "profile": "",
            "reasoning_effort": _validated_openai_reasoning_effort(env.get("OPENAI_REASONING_EFFORT")),
            "approval_policy": "",
            "sandbox_mode": "",
            "extra_args": _clean(env.get("OPENAI_EXTRA_ARGS")) or "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }

    raise ValueError(f"Unsupported provider: {provider}")


def provider_timeout_seconds(*, environ: Mapping[str, str] | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = _clean(env.get("JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS"))
    if raw is None:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS


def asset_provider_timeout_seconds(*, environ: Mapping[str, str] | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = _clean(env.get("JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS"))
    if raw is None:
        raw = _clean(env.get("JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS"))
    if raw is None:
        return DEFAULT_ASSET_PROVIDER_TIMEOUT_SECONDS
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_ASSET_PROVIDER_TIMEOUT_SECONDS


def claude_primary_asset_timeout_seconds(*, environ: Mapping[str, str] | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = _clean(env.get("JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS"))
    if raw is None:
        return DEFAULT_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS


def claude_asset_fallback_provider(*, environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    raw = _clean(env.get("JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER"))
    if raw is None:
        return DEFAULT_CLAUDE_ASSET_FALLBACK_PROVIDER
    return raw


def prompt_mode_settings(mode: str | None) -> dict[str, str | bool | None]:
    normalized = _clean(mode)
    if normalized is None:
        return {
            "search_enabled": False,
            "file_tools_enabled": False,
            "claude_allowed_tools": None,
        }

    if normalized in {"content", "research"}:
        return {
            "search_enabled": True,
            "file_tools_enabled": True,
            "claude_allowed_tools": CLAUDE_RESEARCH_ALLOWED_TOOLS,
        }
    if normalized == "draft":
        return {
            "search_enabled": False,
            "file_tools_enabled": True,
            "claude_allowed_tools": CLAUDE_DRAFT_ALLOWED_TOOLS,
        }
    if normalized == "fix":
        return {
            "search_enabled": False,
            "file_tools_enabled": True,
            "claude_allowed_tools": CLAUDE_FIX_ALLOWED_TOOLS,
        }
    if normalized == "submit":
        return {
            "search_enabled": False,
            "file_tools_enabled": False,
            "claude_allowed_tools": CLAUDE_SUBMIT_ALLOWED_TOOLS,
        }
    raise ValueError(f"Unsupported prompt mode: {mode}")


def provider_command(
    provider: str,
    prompt: str,
    *,
    project_root: Path = PROJECT_ROOT,
    search_enabled: bool = False,
    file_tools_enabled: bool = False,
    claude_allowed_tools: str | None = None,
    json_mode: bool = False,
    json_schema: Mapping[str, object] | None = None,
    json_schema_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = environ if environ is not None else os.environ
    settings = effective_provider_settings(provider, environ=environ)
    if provider == "claude":
        cmd = [
            "claude",
            "--permission-mode",
            settings["permission_mode"],
            "--model",
            settings["model"],
            "--effort",
            settings["effort"],
            "--setting-sources",
            settings["setting_sources"],
            *(
                ["--no-session-persistence"]
                if settings["no_session_persistence"] not in {"0", "false", "False"}
                else []
            ),
            *(
                ["--disable-slash-commands"]
                if settings["disable_slash_commands"] not in {"0", "false", "False"}
                else []
            ),
            *(["--strict-mcp-config"] if settings["strict_mcp_config"] not in {"0", "false", "False"} else []),
            "--mcp-config",
            settings["mcp_config"],
        ]
        if claude_allowed_tools:
            cmd.extend(["--allowedTools", claude_allowed_tools])
        cmd.extend(_split_extra_args(settings.get("extra_args")))
        # Keep print mode immediately before the prompt. Some Claude CLI builds
        # mis-handle `claude -p ... prompt` and ignore the trailing prompt.
        cmd.append("--print")
        cmd.append(prompt)
        return cmd

    if provider == "codex":
        profile_selected = bool(settings["profile"])
        explicit_model = _clean(env.get("CODEX_MODEL")) is not None
        explicit_reasoning_effort = _clean(env.get("CODEX_REASONING_EFFORT")) is not None
        explicit_approval_policy = _clean(env.get("CODEX_APPROVAL_POLICY")) is not None
        explicit_sandbox_mode = _clean(env.get("CODEX_SANDBOX_MODE")) is not None
        cmd = python_script_command(CODEX_EXEC_WRAPPER, environ=env)
        cmd.append("--")
        cmd.append("codex")
        if search_enabled:
            cmd.append("--search")
        if settings["profile"]:
            cmd.extend(["--profile", settings["profile"]])
        # `codex exec` does not accept `--ask-for-approval` or `--sandbox` as
        # separate flags.  Instead, use one of the convenience flags that imply
        # both the approval policy *and* the sandbox mode:
        #   danger-full-access  → --dangerously-bypass-approvals-and-sandbox
        #   workspace-write     → --full-auto  (sandbox=workspace-write, approval=on-request)
        #   read-only           → --sandbox read-only  (no approval flag needed)
        if explicit_approval_policy or explicit_sandbox_mode or not profile_selected:
            sandbox = settings["sandbox_mode"]
            if sandbox == "danger-full-access":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            elif sandbox == "workspace-write":
                cmd.append("--full-auto")
            elif sandbox == "read-only":
                cmd.extend(["--sandbox", "read-only"])
            else:
                # Unknown sandbox mode — pass it through as-is so the user
                # gets a clear error from the codex binary itself.
                cmd.extend(["--sandbox", sandbox])
        cmd.extend(["-C", str(project_root)])
        cmd.extend(
            [
                "exec",
                "--skip-git-repo-check",
            ]
        )
        if explicit_model or not profile_selected:
            cmd.extend(["--model", settings["model"]])
        if explicit_reasoning_effort or not profile_selected:
            cmd.extend(["-c", f'model_reasoning_effort="{settings["reasoning_effort"]}"'])
        cmd.extend(_split_extra_args(settings.get("extra_args")))
        cmd.append(prompt)
        return cmd

    if provider in ("gemini", "gemini-flash"):
        cmd = [
            "gemini",
            "--yolo",
            "--model",
            settings["model"],
        ]
        cmd.extend(_split_extra_args(settings.get("extra_args")))
        cmd.append("-p")
        cmd.append(prompt)
        return cmd

    if provider == "openai":
        cmd = python_script_command(OPENAI_PROVIDER_SCRIPT, environ=env)
        cmd.extend(["--model", settings["model"]])
        if settings["reasoning_effort"]:
            cmd.extend(["--reasoning-effort", settings["reasoning_effort"]])
        if search_enabled:
            cmd.append("--search")
        if file_tools_enabled:
            cmd.append("--file-tools")
        if json_schema is not None:
            cmd.extend(["--json-schema", json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))])
            if json_schema_name:
                cmd.extend(["--json-schema-name", json_schema_name])
        elif json_mode:
            cmd.append("--json-mode")
        extra = _split_extra_args(settings.get("extra_args"))
        if extra:
            cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    raise ValueError(f"Unsupported provider: {provider}")


def provider_command_for_mode(
    provider: str,
    prompt: str,
    *,
    mode: str,
    project_root: Path = PROJECT_ROOT,
    json_mode: bool = False,
    json_schema: Mapping[str, object] | None = None,
    json_schema_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    mode_settings = prompt_mode_settings(mode)
    return provider_command(
        provider,
        prompt,
        project_root=project_root,
        search_enabled=bool(mode_settings["search_enabled"]),
        file_tools_enabled=bool(mode_settings["file_tools_enabled"]),
        claude_allowed_tools=mode_settings["claude_allowed_tools"],
        json_mode=json_mode,
        json_schema=json_schema,
        json_schema_name=json_schema_name,
        environ=environ,
    )


def shell_exports(provider: str, *, environ: Mapping[str, str] | None = None) -> str:
    settings = effective_provider_settings(provider, environ=environ)
    exports = {
        "JOB_ASSETS_PROVIDER_MODEL": settings["model"],
        "JOB_ASSETS_PROVIDER_EFFORT": settings["effort"],
        "JOB_ASSETS_PROVIDER_PERMISSION_MODE": settings.get("permission_mode", ""),
        "JOB_ASSETS_CLAUDE_SETTING_SOURCES": settings.get("setting_sources", ""),
        "JOB_ASSETS_CLAUDE_NO_SESSION_PERSISTENCE": settings.get("no_session_persistence", ""),
        "JOB_ASSETS_CLAUDE_DISABLE_SLASH_COMMANDS": settings.get("disable_slash_commands", ""),
        "JOB_ASSETS_CLAUDE_STRICT_MCP_CONFIG": settings.get("strict_mcp_config", ""),
        "JOB_ASSETS_CLAUDE_MCP_CONFIG": settings.get("mcp_config", ""),
        "JOB_ASSETS_PROVIDER_EXTRA_ARGS": settings.get("extra_args", ""),
        "JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS": settings.get("asset_primary_timeout_seconds", ""),
        "JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER": settings.get("asset_fallback_provider", ""),
        "JOB_ASSETS_PROVIDER_PROFILE": settings["profile"],
        "JOB_ASSETS_PROVIDER_REASONING_EFFORT": settings["reasoning_effort"],
        "JOB_ASSETS_PROVIDER_APPROVAL_POLICY": settings.get("approval_policy", ""),
        "JOB_ASSETS_PROVIDER_SANDBOX_MODE": settings.get("sandbox_mode", ""),
        "JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS": settings["timeout_seconds"],
        "JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS": settings["asset_timeout_seconds"],
    }
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in exports.items())


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve shared LLM provider defaults.")
    parser.add_argument("provider", nargs="?", choices=("claude", "codex", "gemini", "openai"))
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print shell export statements for the effective provider settings.",
    )
    parser.add_argument(
        "--command",
        action="store_true",
        help="Print the resolved provider command as NUL-delimited argv.",
    )
    parser.add_argument(
        "--prompt-file",
        default="",
        help="Path to the prompt file when using --command.",
    )
    parser.add_argument(
        "--mode",
        choices=("content", "research", "draft", "fix", "submit"),
        default="",
        help="Optional execution mode that resolves shared search/tool defaults.",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Project root to use for provider command generation.",
    )
    parser.add_argument(
        "--search-enabled",
        action="store_true",
        help="Enable the provider's search/web-research execution mode for --command.",
    )
    parser.add_argument(
        "--claude-allowed-tools",
        default="",
        help="Optional Claude --allowedTools value for --command.",
    )
    parser.add_argument(
        "--automation-chain",
        action="store_true",
        help="Print the resolved automation fallback chain as a comma-separated list.",
    )
    args = parser.parse_args()

    if args.automation_chain:
        print(",".join(automation_provider_chain()))
        return 0

    if not args.provider:
        parser.error("provider is required unless --automation-chain is used")

    if args.command:
        if not args.prompt_file:
            parser.error("--prompt-file is required with --command")
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        mode_settings = prompt_mode_settings(args.mode)
        if args.search_enabled or _clean(args.claude_allowed_tools):
            command = provider_command(
                args.provider,
                prompt,
                project_root=Path(args.project_root),
                search_enabled=args.search_enabled or bool(mode_settings["search_enabled"]),
                file_tools_enabled=bool(mode_settings["file_tools_enabled"]),
                claude_allowed_tools=_clean(args.claude_allowed_tools) or mode_settings["claude_allowed_tools"],
            )
        else:
            command = provider_command_for_mode(
                args.provider,
                prompt,
                project_root=Path(args.project_root),
                mode=args.mode or "draft",
            )
        sys.stdout.buffer.write("\0".join(command).encode("utf-8"))
        sys.stdout.buffer.write(b"\0")
        return 0

    if args.shell:
        print(shell_exports(args.provider))
        return 0

    settings = effective_provider_settings(args.provider)
    for key, value in settings.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
