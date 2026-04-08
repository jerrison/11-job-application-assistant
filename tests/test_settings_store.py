import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SettingsStoreTests(unittest.TestCase):
    def test_load_bootstrap_reports_required_material_and_credential_state(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            summary = settings_store.load_bootstrap(environ={"JOB_ASSETS_APP_HOME": str(runtime_root)})

            self.assertFalse(summary["onboarding"]["complete"])
            self.assertFalse(summary["onboarding"]["required_materials"]["master_resume"])
            self.assertFalse(summary["onboarding"]["credentials_ready"])

            (runtime_root / "master_resume.md").write_text("# Resume\n", encoding="utf-8")
            (runtime_root / ".env.local").write_text('OPENAI_API_KEY="sk-test-12345678"\n', encoding="utf-8")

            ready_summary = settings_store.load_bootstrap(environ={"JOB_ASSETS_APP_HOME": str(runtime_root)})

        self.assertTrue(ready_summary["onboarding"]["required_materials"]["master_resume"])
        self.assertTrue(ready_summary["onboarding"]["credentials_ready"])
        self.assertTrue(ready_summary["onboarding"]["complete"])

    def test_load_bootstrap_requires_non_empty_optional_material_content(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            (runtime_root / "master_resume.md").write_text("# Resume\n", encoding="utf-8")
            (runtime_root / ".env.local").write_text('OPENAI_API_KEY="sk-test-12345678"\n', encoding="utf-8")
            (runtime_root / "application_profile.md").write_text("", encoding="utf-8")

            summary = settings_store.load_bootstrap(environ={"JOB_ASSETS_APP_HOME": str(runtime_root)})

        self.assertTrue(summary["onboarding"]["required_materials"]["master_resume"])
        self.assertTrue(summary["onboarding"]["credentials_ready"])
        self.assertFalse(summary["onboarding"]["recommended_materials"]["application_profile"])
        self.assertFalse(summary["materials"]["application_profile"]["has_content"])

    def test_load_settings_reads_materials_and_redacts_credentials(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            (runtime_root / "master_resume.md").write_text("# Resume\n", encoding="utf-8")
            (runtime_root / "application_profile.md").write_text("email: test@example.com\n", encoding="utf-8")
            (runtime_root / ".env.local").write_text(
                'ASSET_LLM_PROVIDER="gemini"\nOPENAI_API_KEY="sk-test-12345678"\nSTEEL_LOCAL="true"\n',
                encoding="utf-8",
            )

            settings = settings_store.load_settings(environ={"JOB_ASSETS_APP_HOME": str(runtime_root)})

        self.assertEqual(settings["materials"]["master_resume"]["content"], "# Resume\n")
        self.assertEqual(settings["materials"]["application_profile"]["content"], "email: test@example.com\n")
        self.assertEqual(settings["providers"]["default_provider"], "gemini")
        self.assertTrue(settings["providers"]["steel_local"])
        self.assertTrue(settings["credentials"]["openai_api_key"]["configured"])
        self.assertNotEqual(settings["credentials"]["openai_api_key"]["preview"], "sk-test-12345678")
        self.assertTrue(settings["credentials"]["openai_api_key"]["preview"].startswith("sk-t"))

    def test_save_settings_writes_materials_and_updates_env_file(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            environ = {"JOB_ASSETS_APP_HOME": str(runtime_root)}

            settings = settings_store.save_settings(
                {
                    "materials": {
                        "master_resume": "# Updated Resume\n",
                        "candidate_context": "Motivation details\n",
                    },
                    "providers": {
                        "default_provider": "openai",
                        "provider_chain": "openai,gemini",
                        "openai_model": "gpt-5.4",
                        "steel_local": True,
                        "steel_base_url": "http://localhost:3000",
                    },
                    "credentials": {
                        "openai_api_key": "sk-live-12345678",
                        "steel_api_key": "steel-secret",
                    },
                },
                environ=environ,
            )

            env_file_text = (runtime_root / ".env.local").read_text(encoding="utf-8")
            self.assertEqual((runtime_root / "master_resume.md").read_text(encoding="utf-8"), "# Updated Resume\n")
            self.assertEqual((runtime_root / "candidate_context.md").read_text(encoding="utf-8"), "Motivation details\n")
            self.assertIn('ASSET_LLM_PROVIDER="openai"', env_file_text)
            self.assertIn('ASSET_LLM_PROVIDER_CHAIN="openai,gemini"', env_file_text)
            self.assertIn('OPENAI_MODEL="gpt-5.4"', env_file_text)
            self.assertIn('STEEL_LOCAL="true"', env_file_text)
            self.assertIn('STEEL_BASE_URL="http://localhost:3000"', env_file_text)
            self.assertIn('OPENAI_API_KEY="sk-live-12345678"', env_file_text)
            self.assertIn('STEEL_API_KEY="steel-secret"', env_file_text)
            self.assertEqual(environ["ASSET_LLM_PROVIDER"], "openai")
            self.assertEqual(environ["STEEL_LOCAL"], "true")
            self.assertTrue(settings["credentials"]["openai_api_key"]["configured"])
            self.assertTrue(settings["credentials"]["steel_api_key"]["configured"])

    def test_save_settings_emits_redacted_runtime_trace(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            environ = {"JOB_ASSETS_APP_HOME": str(runtime_root)}

            settings_store.save_settings(
                {
                    "materials": {
                        "master_resume": "# Updated Resume\n",
                    },
                    "credentials": {
                        "openai_api_key": "sk-live-12345678",
                    },
                },
                environ=environ,
            )

            trace_lines = (runtime_root / "traces" / "runtime-trace.jsonl").read_text(encoding="utf-8").splitlines()
            trace_payloads = [json.loads(line) for line in trace_lines]

        self.assertTrue(any(payload["event_type"] == "settings_saved" for payload in trace_payloads))
        self.assertTrue(any(payload["action"] == "settings_save" for payload in trace_payloads))
        self.assertFalse(any("sk-live-12345678" in line for line in trace_lines))

    def test_import_material_persists_content_and_returns_bootstrap(self):
        settings_store = load_module("settings_store", "scripts/settings_store.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            environ = {
                "JOB_ASSETS_APP_HOME": str(runtime_root),
                "OPENAI_API_KEY": "sk-test-12345678",
            }

            payload = settings_store.import_material(
                "master_resume",
                text="# Imported Resume\n",
                environ=environ,
            )

        self.assertEqual(payload["material_key"], "master_resume")
        self.assertEqual(payload["text"], "# Imported Resume\n")
        self.assertEqual(payload["settings"]["materials"]["master_resume"]["content"], "# Imported Resume\n")
        self.assertTrue(payload["bootstrap"]["onboarding"]["required_materials"]["master_resume"])
        self.assertTrue(payload["bootstrap"]["onboarding"]["credentials_ready"])


if __name__ == "__main__":
    unittest.main()
