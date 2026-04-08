import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CODEX_EXEC_WRAPPER = PROJECT_ROOT / "scripts" / "codex_exec_wrapper.py"


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LlmProviderTests(unittest.TestCase):
    def test_default_active_provider_defaults_to_openai(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        self.assertEqual(provider.default_active_provider(environ={}), "openai")

    def test_default_provider_chain_defaults_to_openai_without_env(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        self.assertEqual(provider.default_provider_chain(environ={}), "openai")

    def test_automation_provider_chain_filters_to_openai_and_gemini(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        chain = provider.automation_provider_chain(
            environ={"ASSET_LLM_PROVIDER": "openai", "ASSET_LLM_PROVIDER_CHAIN": "openai,gemini,claude,codex"}
        )

        self.assertEqual(chain, ("openai", "gemini"))

    def test_automation_provider_chain_preserves_allowed_order_and_appends_missing_allowed_provider(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        chain = provider.automation_provider_chain(
            environ={"ASSET_LLM_PROVIDER": "gemini", "ASSET_LLM_PROVIDER_CHAIN": "gemini"}
        )

        self.assertEqual(chain, ("gemini", "openai"))

    def test_effective_provider_settings_defaults_to_high_reasoning_models(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        claude = provider.effective_provider_settings("claude", environ={})
        codex = provider.effective_provider_settings("codex", environ={})

        self.assertEqual(claude["model"], "claude-sonnet-4-6")
        self.assertEqual(claude["effort"], "max")
        self.assertEqual(claude["permission_mode"], "auto")
        self.assertEqual(claude["timeout_seconds"], "600")
        self.assertEqual(claude["asset_timeout_seconds"], "1200")
        self.assertEqual(claude["asset_primary_timeout_seconds"], "600")
        self.assertEqual(claude["asset_fallback_provider"], "")
        self.assertEqual(codex["model"], "gpt-5.4")
        self.assertEqual(codex["reasoning_effort"], "xhigh")
        self.assertEqual(codex["approval_policy"], "never")
        self.assertEqual(codex["sandbox_mode"], "danger-full-access")
        self.assertEqual(codex["timeout_seconds"], "600")
        self.assertEqual(codex["asset_timeout_seconds"], "1200")
        self.assertEqual(codex["asset_primary_timeout_seconds"], "")
        self.assertEqual(codex["asset_fallback_provider"], "")

    def test_effective_provider_settings_respects_explicit_overrides(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        environ = {
            "CLAUDE_MODEL": "claude-sonnet-4-6",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_PERMISSION_MODE": "acceptEdits",
            "CODEX_MODEL": "gpt-5.3-codex",
            "CODEX_REASONING_EFFORT": "high",
            "CODEX_APPROVAL_POLICY": "on-request",
            "CODEX_SANDBOX_MODE": "workspace-write",
            "CODEX_PROFILE": "local-fast",
            "JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS": "42",
            "JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS": "84",
            "JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS": "99",
            "JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER": "codex-alt",
        }

        claude = provider.effective_provider_settings("claude", environ=environ)
        codex = provider.effective_provider_settings("codex", environ=environ)

        self.assertEqual(claude["model"], "claude-sonnet-4-6")
        self.assertEqual(claude["effort"], "high")
        self.assertEqual(claude["permission_mode"], "acceptEdits")
        self.assertEqual(claude["timeout_seconds"], "42")
        self.assertEqual(claude["asset_timeout_seconds"], "84")
        self.assertEqual(claude["asset_primary_timeout_seconds"], "99")
        self.assertEqual(claude["asset_fallback_provider"], "codex-alt")
        self.assertEqual(codex["model"], "gpt-5.3-codex")
        self.assertEqual(codex["reasoning_effort"], "high")
        self.assertEqual(codex["approval_policy"], "on-request")
        self.assertEqual(codex["sandbox_mode"], "workspace-write")
        self.assertEqual(codex["profile"], "local-fast")
        self.assertEqual(codex["timeout_seconds"], "42")
        self.assertEqual(codex["asset_timeout_seconds"], "84")

    def test_provider_command_builds_codex_exec_with_tui_like_execution_settings(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "codex",
            "Draft the tailored resume.",
            search_enabled=True,
            project_root=PROJECT_ROOT,
            environ={},
        )

        self.assertEqual(
            command,
            [
                sys.executable,
                str(CODEX_EXEC_WRAPPER),
                "--",
                "codex",
                "--search",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(PROJECT_ROOT),
                "exec",
                "--skip-git-repo-check",
                "--model",
                "gpt-5.4",
                "-c",
                'model_reasoning_effort="xhigh"',
                "Draft the tailored resume.",
            ],
        )

    def test_provider_command_builds_claude_exec_with_project_local_settings_only(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "claude",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertEqual(
            command,
            [
                "claude",
                "--permission-mode",
                "auto",
                "--model",
                "claude-sonnet-4-6",
                "--effort",
                "max",
                "--setting-sources",
                "project,local",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--print",
                "Draft the tailored resume.",
            ],
        )

    def test_provider_command_places_claude_print_immediately_before_prompt(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "claude",
            "Reply with only OK.",
            environ={},
        )

        self.assertEqual(command[-2:], ["--print", "Reply with only OK."])
        self.assertNotIn("-p", command)

    def test_provider_command_includes_claude_allowed_tools_when_requested(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "claude",
            "Draft the tailored resume.",
            claude_allowed_tools=provider.CLAUDE_DRAFT_ALLOWED_TOOLS,
            environ={},
        )

        allowed_index = command.index("--allowedTools")
        self.assertEqual(
            command[allowed_index : allowed_index + 2],
            ["--allowedTools", provider.CLAUDE_DRAFT_ALLOWED_TOOLS],
        )
        self.assertEqual(command[-2:], ["--print", "Draft the tailored resume."])

    def test_prompt_mode_settings_centralize_search_and_tool_defaults(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        self.assertEqual(
            provider.prompt_mode_settings("content"),
            {
                "search_enabled": True,
                "file_tools_enabled": True,
                "claude_allowed_tools": provider.CLAUDE_RESEARCH_ALLOWED_TOOLS,
            },
        )
        self.assertEqual(
            provider.prompt_mode_settings("draft"),
            {
                "search_enabled": False,
                "file_tools_enabled": True,
                "claude_allowed_tools": provider.CLAUDE_DRAFT_ALLOWED_TOOLS,
            },
        )
        self.assertEqual(
            provider.prompt_mode_settings("fix"),
            {
                "search_enabled": False,
                "file_tools_enabled": True,
                "claude_allowed_tools": provider.CLAUDE_FIX_ALLOWED_TOOLS,
            },
        )
        self.assertEqual(
            provider.prompt_mode_settings("submit"),
            {
                "search_enabled": False,
                "file_tools_enabled": False,
                "claude_allowed_tools": provider.CLAUDE_SUBMIT_ALLOWED_TOOLS,
            },
        )

    def test_command_cli_emits_nul_delimited_mode_specific_argv(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_file = Path(tmp_dir) / "prompt.txt"
            prompt_file.write_text("Draft the tailored resume.", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "llm_provider.py"),
                    "codex",
                    "--command",
                    "--mode",
                    "research",
                    "--prompt-file",
                    str(prompt_file),
                    "--project-root",
                    str(PROJECT_ROOT),
                ],
                capture_output=True,
                check=True,
            )

        argv = [part.decode("utf-8") for part in completed.stdout.split(b"\0") if part]
        self.assertEqual(
            argv,
            [
                sys.executable,
                str(CODEX_EXEC_WRAPPER),
                "--",
                "codex",
                "--search",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(PROJECT_ROOT),
                "exec",
                "--skip-git-repo-check",
                "--model",
                "gpt-5.4",
                "-c",
                'model_reasoning_effort="xhigh"',
                "Draft the tailored resume.",
            ],
        )

    def test_asset_timeout_falls_back_to_general_timeout_override(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        claude = provider.effective_provider_settings(
            "claude",
            environ={"JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS": "33"},
        )

        self.assertEqual(claude["timeout_seconds"], "33")
        self.assertEqual(claude["asset_timeout_seconds"], "33")

    def test_codex_settings_can_fall_back_to_runtime_config(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                'model = "gpt-5.5"\n'
                'model_reasoning_effort = "high"\n'
                'approval_policy = "on-request"\n'
                'sandbox_mode = "workspace-write"\n',
                encoding="utf-8",
            )

            codex = provider.effective_provider_settings(
                "codex",
                environ={"JOB_ASSETS_CODEX_CONFIG_PATH": str(config_path)},
            )

        self.assertEqual(codex["model"], "gpt-5.5")
        self.assertEqual(codex["reasoning_effort"], "high")
        self.assertEqual(codex["approval_policy"], "on-request")
        self.assertEqual(codex["sandbox_mode"], "workspace-write")

    def test_provider_command_lets_codex_profile_control_core_execution_settings(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "codex",
            "Draft the tailored resume.",
            project_root=PROJECT_ROOT,
            environ={"CODEX_PROFILE": "local-fast"},
        )

        self.assertEqual(
            command,
            [
                sys.executable,
                str(CODEX_EXEC_WRAPPER),
                "--",
                "codex",
                "--profile",
                "local-fast",
                "-C",
                str(PROJECT_ROOT),
                "exec",
                "--skip-git-repo-check",
                "Draft the tailored resume.",
            ],
        )

    def test_provider_command_allows_extra_provider_args(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        claude_command = provider.provider_command(
            "claude",
            "Draft the tailored resume.",
            environ={"CLAUDE_EXTRA_ARGS": "--output-format json --verbose"},
        )
        codex_command = provider.provider_command(
            "codex",
            "Draft the tailored resume.",
            project_root=PROJECT_ROOT,
            environ={"CODEX_EXTRA_ARGS": "--json --color never"},
        )

        self.assertEqual(
            claude_command[-5:], ["--output-format", "json", "--verbose", "--print", "Draft the tailored resume."]
        )
        self.assertEqual(codex_command[-4:], ["--json", "--color", "never", "Draft the tailored resume."])

    def test_provider_command_codex_workspace_write_uses_full_auto(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "codex",
            "Draft the tailored resume.",
            project_root=PROJECT_ROOT,
            environ={"CODEX_SANDBOX_MODE": "workspace-write"},
        )

        self.assertIn("--full-auto", command)
        self.assertNotIn("--ask-for-approval", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_provider_command_codex_read_only_uses_sandbox_flag(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "codex",
            "Draft the tailored resume.",
            project_root=PROJECT_ROOT,
            environ={"CODEX_SANDBOX_MODE": "read-only"},
        )

        idx = command.index("--sandbox")
        self.assertEqual(command[idx + 1], "read-only")
        self.assertNotIn("--ask-for-approval", command)
        self.assertNotIn("--full-auto", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_codex_exec_wrapper_preserves_mcp_and_strips_apps_features(self):
        wrapper = load_module("codex_exec_wrapper", "scripts/codex_exec_wrapper.py")

        sanitized = wrapper._sanitize_config(
            'model = "gpt-5.4"\n'
            "[mcp_servers.context7]\n"
            'command = "npx"\n'
            '[projects."/repo"]\n'
            'trust_level = "trusted"\n'
            "[features]\n"
            "unified_exec = true\n"
            "apps = true\n"
            "[mcp_servers.playwright]\n"
            'command = "npx"\n'
        )

        self.assertIn("[mcp_servers.context7]", sanitized)
        self.assertIn("[mcp_servers.playwright]", sanitized)
        self.assertNotIn("apps = true", sanitized)
        self.assertIn('model = "gpt-5.4"', sanitized)
        self.assertIn('[projects."/repo"]', sanitized)
        self.assertIn("[features]", sanitized)
        self.assertIn("unified_exec = true", sanitized)

    def test_codex_exec_wrapper_copies_global_assets_into_isolated_home(self):
        wrapper = load_module("codex_exec_wrapper", "scripts/codex_exec_wrapper.py")

        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source_home = Path(source_tmp)
            target_home = Path(target_tmp)
            (source_home / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
            (source_home / "AGENTS.md").write_text("# Global instructions\n", encoding="utf-8")
            (source_home / "prompts").mkdir()
            (source_home / "prompts" / "ce-plan.md").write_text("prompt body\n", encoding="utf-8")
            (source_home / "skills").mkdir()
            (source_home / "skills" / "ce:plan").mkdir()
            (source_home / "skills" / "ce:plan" / "SKILL.md").write_text("skill body\n", encoding="utf-8")
            (source_home / "config.toml").write_text(
                'model = "gpt-5.4"\n[mcp_servers.context7]\nurl = "https://mcp.context7.com/mcp"\n',
                encoding="utf-8",
            )

            wrapper._copy_codex_home_artifacts(source_home, target_home)

            self.assertEqual((target_home / "auth.json").read_text(encoding="utf-8"), '{"token":"secret"}')
            self.assertEqual((target_home / "AGENTS.md").read_text(encoding="utf-8"), "# Global instructions\n")
            self.assertEqual(
                (target_home / "prompts" / "ce-plan.md").read_text(encoding="utf-8"),
                "prompt body\n",
            )
            self.assertEqual(
                (target_home / "skills" / "ce:plan" / "SKILL.md").read_text(encoding="utf-8"),
                "skill body\n",
            )
            sanitized_config = (target_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.4"', sanitized_config)
            self.assertIn("[mcp_servers.context7]", sanitized_config)

    def test_shell_exports_include_effective_values(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        exports = provider.shell_exports("claude", environ={})

        self.assertIn("export JOB_ASSETS_PROVIDER_MODEL=claude-sonnet-4-6", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_EFFORT=max", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_PERMISSION_MODE=auto", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_SETTING_SOURCES=project,local", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_NO_SESSION_PERSISTENCE=1", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_DISABLE_SLASH_COMMANDS=1", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_STRICT_MCP_CONFIG=1", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_MCP_CONFIG='{\"mcpServers\":{}}'", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_EXTRA_ARGS=''", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS=600", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER=''", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_APPROVAL_POLICY=", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_SANDBOX_MODE=", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS=600", exports)
        self.assertIn("export JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS=1200", exports)

        codex_exports = provider.shell_exports("codex", environ={})
        self.assertIn("export JOB_ASSETS_PROVIDER_APPROVAL_POLICY=never", codex_exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_SANDBOX_MODE=danger-full-access", codex_exports)

    def test_effective_provider_settings_gemini_defaults_to_flash(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        gemini = provider.effective_provider_settings("gemini", environ={})

        self.assertEqual(gemini["model"], "gemini-3-flash-preview")
        self.assertEqual(gemini["effort"], "")
        self.assertEqual(gemini["profile"], "")
        self.assertEqual(gemini["reasoning_effort"], "")
        self.assertEqual(gemini["extra_args"], "")
        self.assertEqual(gemini["timeout_seconds"], "600")
        self.assertEqual(gemini["asset_timeout_seconds"], "1200")
        self.assertEqual(gemini["asset_primary_timeout_seconds"], "")
        self.assertEqual(gemini["asset_fallback_provider"], "")

    def test_effective_provider_settings_gemini_respects_overrides(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        environ = {
            "GEMINI_MODEL": "gemini-3-flash-preview",
            "GEMINI_EXTRA_ARGS": "--sandbox",
            "JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS": "42",
        }

        gemini = provider.effective_provider_settings("gemini", environ=environ)

        self.assertEqual(gemini["model"], "gemini-3-flash-preview")
        self.assertEqual(gemini["extra_args"], "--sandbox")
        self.assertEqual(gemini["timeout_seconds"], "42")

    def test_provider_command_builds_gemini_with_yolo_and_prompt(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertEqual(
            command,
            [
                "gemini",
                "--yolo",
                "--model",
                "gemini-3-flash-preview",
                "-p",
                "Draft the tailored resume.",
            ],
        )

    def test_provider_command_gemini_ignores_claude_specific_flags(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini",
            "Draft the tailored resume.",
            search_enabled=True,
            claude_allowed_tools=provider.CLAUDE_DRAFT_ALLOWED_TOOLS,
            environ={},
        )

        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("--search", command)
        self.assertNotIn("--permission-mode", command)
        self.assertIn("gemini", command)
        self.assertIn("--yolo", command)

    def test_provider_command_gemini_allows_extra_args(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini",
            "Draft the tailored resume.",
            environ={"GEMINI_EXTRA_ARGS": "--sandbox --output-format json"},
        )

        self.assertIn("--sandbox", command)
        self.assertIn("--output-format", command)
        self.assertIn("json", command)
        self.assertEqual(command[-2:], ["-p", "Draft the tailored resume."])

    def test_provider_command_gemini_uses_flash_model(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini",
            "Draft the tailored resume.",
            environ={},
        )

        model_idx = command.index("--model")
        self.assertEqual(command[model_idx + 1], "gemini-3-flash-preview")

    def test_shell_exports_gemini_includes_model_and_empty_irrelevant_keys(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        exports = provider.shell_exports("gemini", environ={})

        self.assertIn("export JOB_ASSETS_PROVIDER_MODEL=gemini-3-flash-preview", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_EFFORT=''", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_PERMISSION_MODE=''", exports)
        self.assertIn("export JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS=600", exports)
        self.assertIn("export JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS=1200", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS=''", exports)
        self.assertIn("export JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER=''", exports)

    def test_command_cli_accepts_gemini_provider(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_file = Path(tmp_dir) / "prompt.txt"
            prompt_file.write_text("Draft the tailored resume.", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "llm_provider.py"),
                    "gemini",
                    "--command",
                    "--prompt-file",
                    str(prompt_file),
                ],
                capture_output=True,
                check=True,
            )

        argv = [part.decode("utf-8") for part in completed.stdout.split(b"\0") if part]
        self.assertEqual(argv[0], "gemini")
        self.assertIn("--yolo", argv)
        self.assertIn("-p", argv)
        self.assertEqual(argv[-1], "Draft the tailored resume.")

    def test_valid_providers_constant_includes_gemini_flash(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        self.assertIn("gemini", provider.VALID_PROVIDERS)
        self.assertIn("gemini-flash", provider.VALID_PROVIDERS)
        self.assertIn("claude", provider.VALID_PROVIDERS)
        self.assertIn("codex", provider.VALID_PROVIDERS)

    def test_effective_provider_settings_gemini_flash_defaults(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        flash = provider.effective_provider_settings("gemini-flash", environ={})

        self.assertEqual(flash["model"], "gemini-3-flash-preview")
        self.assertEqual(flash["timeout_seconds"], "600")
        self.assertEqual(flash["asset_timeout_seconds"], "1200")

    def test_effective_provider_settings_gemini_flash_respects_overrides(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        environ = {"GEMINI_FLASH_MODEL": "gemini-3.1-flash"}

        flash = provider.effective_provider_settings("gemini-flash", environ=environ)

        self.assertEqual(flash["model"], "gemini-3.1-flash")

    def test_provider_command_gemini_flash_uses_gemini_binary(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini-flash",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertEqual(command[0], "gemini")
        self.assertIn("--model", command)
        self.assertIn("gemini-3-flash-preview", command)
        self.assertEqual(command[-2:], ["-p", "Draft the tailored resume."])

    def test_effective_provider_settings_openai_defaults(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        openai = provider.effective_provider_settings("openai", environ={})

        self.assertEqual(openai["model"], "gpt-5.4")
        self.assertEqual(openai["effort"], "")
        self.assertEqual(openai["profile"], "")
        self.assertEqual(openai["reasoning_effort"], "")
        self.assertEqual(openai["extra_args"], "")
        self.assertEqual(openai["timeout_seconds"], "600")
        self.assertEqual(openai["asset_timeout_seconds"], "1200")
        self.assertEqual(openai["asset_primary_timeout_seconds"], "")
        self.assertEqual(openai["asset_fallback_provider"], "")

    def test_effective_provider_settings_reads_openai_reasoning_effort(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        openai = provider.effective_provider_settings(
            "openai",
            environ={"OPENAI_MODEL": "gpt-5.4", "OPENAI_REASONING_EFFORT": "xhigh"},
        )

        self.assertEqual(openai["model"], "gpt-5.4")
        self.assertEqual(openai["reasoning_effort"], "xhigh")

    def test_effective_provider_settings_rejects_invalid_openai_reasoning_effort(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        with self.assertRaisesRegex(ValueError, "Unsupported OpenAI reasoning effort"):
            provider.effective_provider_settings(
                "openai",
                environ={"OPENAI_REASONING_EFFORT": "maximum"},
            )

    def test_provider_command_openai_basic(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "openai",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(PROJECT_ROOT / "scripts" / "openai_provider.py"))
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4", command)
        self.assertEqual(command[-1], "Draft the tailored resume.")

    def test_provider_command_openai_with_search(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "openai",
            "Research the company.",
            search_enabled=True,
            environ={},
        )

        self.assertIn("--search", command)
        self.assertEqual(command[-1], "Research the company.")

    def test_provider_command_builds_openai_exec_with_reasoning_effort(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "openai",
            "Reply with OK.",
            environ={"OPENAI_REASONING_EFFORT": "xhigh"},
        )

        self.assertIn("--reasoning-effort", command)
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "xhigh")

    def test_provider_binary_openai(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        binary = provider.provider_binary("openai")

        self.assertEqual(binary, sys.executable)

    def test_provider_available_openai_requires_api_key(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        with (
            patch.object(provider.shutil, "which", return_value=sys.executable),
            patch.object(provider.importlib.util, "find_spec", return_value=object()),
        ):
            self.assertFalse(provider.provider_available("openai", environ={}))

    def test_provider_available_openai_requires_package(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        with (
            patch.object(provider.shutil, "which", return_value=sys.executable),
            patch.object(provider.importlib.util, "find_spec", return_value=None),
        ):
            self.assertFalse(provider.provider_available("openai", environ={"OPENAI_API_KEY": "sk-test"}))

    def test_provider_available_openai_ready_when_key_and_package_present(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        with (
            patch.object(provider.shutil, "which", return_value=sys.executable),
            patch.object(provider.importlib.util, "find_spec", return_value=object()),
        ):
            self.assertTrue(provider.provider_available("openai", environ={"OPENAI_API_KEY": "sk-test"}))

    def test_provider_available_openai_ready_when_key_pool_and_package_present(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        with (
            patch.object(provider.shutil, "which", return_value=sys.executable),
            patch.object(provider.importlib.util, "find_spec", return_value=object()),
        ):
            self.assertTrue(provider.provider_available("openai", environ={"OPENAI_API_KEYS": "sk-a,sk-b"}))

    def test_provider_command_openai_with_file_tools(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "openai",
            "Draft the tailored resume.",
            file_tools_enabled=True,
            environ={},
        )

        self.assertIn("--file-tools", command)
        self.assertEqual(command[-1], "Draft the tailored resume.")

    def test_provider_command_openai_no_file_tools_by_default(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "openai",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertNotIn("--file-tools", command)

    def test_provider_command_openai_json_schema(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        schema = {
            "type": "object",
            "properties": {"why_company": {"type": "string"}},
            "required": ["why_company"],
            "additionalProperties": False,
        }

        command = provider.provider_command(
            "openai",
            "Return structured answers.",
            json_schema=schema,
            json_schema_name="application_answers",
            environ={},
        )

        self.assertIn("--json-schema", command)
        self.assertIn("--json-schema-name", command)
        schema_index = command.index("--json-schema")
        parsed_schema = json.loads(command[schema_index + 1])
        self.assertEqual(parsed_schema, schema)
        name_index = command.index("--json-schema-name")
        self.assertEqual(command[name_index + 1], "application_answers")

    def test_provider_command_for_mode_openai_draft_gets_file_tools(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "openai",
            "Draft the tailored resume.",
            mode="draft",
            environ={},
        )

        self.assertIn("--file-tools", command)
        self.assertNotIn("--search", command)

    def test_provider_command_for_mode_openai_fix_gets_file_tools(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "openai",
            "Fix the resume.",
            mode="fix",
            environ={},
        )

        self.assertIn("--file-tools", command)
        self.assertNotIn("--search", command)

    def test_provider_command_for_mode_openai_research_gets_both(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "openai",
            "Research the company.",
            mode="research",
            environ={},
        )

        self.assertIn("--search", command)
        self.assertIn("--file-tools", command)

    def test_provider_command_for_mode_openai_content_gets_both(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "openai",
            "Generate content.",
            mode="content",
            environ={},
        )

        self.assertIn("--search", command)
        self.assertIn("--file-tools", command)

    def test_provider_command_for_mode_openai_submit_no_file_tools(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "openai",
            "Submit the application.",
            mode="submit",
            environ={},
        )

        self.assertNotIn("--file-tools", command)
        self.assertNotIn("--search", command)

    def test_provider_command_for_mode_codex_submit_ignores_json_schema_flags(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")
        schema = {
            "type": "object",
            "properties": {"why_company": {"type": "string"}},
            "required": ["why_company"],
            "additionalProperties": False,
        }

        command = provider.provider_command_for_mode(
            "codex",
            "Submit the application.",
            mode="submit",
            json_schema=schema,
            json_schema_name="application_answers",
            environ={},
        )

        self.assertNotIn("--json-schema", command)
        self.assertNotIn("--json-schema-name", command)

    def test_provider_command_for_mode_claude_unaffected_by_file_tools(self):
        """File tools flag should not affect Claude commands."""
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command_for_mode(
            "claude",
            "Draft the tailored resume.",
            mode="draft",
            environ={},
        )

        self.assertNotIn("--file-tools", command)


if __name__ == "__main__":
    unittest.main()
