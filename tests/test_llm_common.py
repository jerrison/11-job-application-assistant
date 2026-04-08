import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LlmCommonPromptTests(unittest.TestCase):
    def test_research_prompt_forbids_recursive_repo_entrypoints(self):
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                "source scripts/llm_common.sh; prompt=$(mktemp); "
                'job_assets_write_company_research_prompt "$prompt" starburst senior-pm output/starburst output/starburst/content; '
                'cat "$prompt"',
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            "Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py",
            completed.stdout,
        )
        self.assertIn("research_cache.json", completed.stdout)
        self.assertIn("Do NOT include role_context", completed.stdout)

    def test_drafting_prompt_reads_research_cache_and_forbids_recursive_repo_entrypoints(self):
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                "source scripts/llm_common.sh; prompt=$(mktemp); "
                'job_assets_write_drafting_prompt "$prompt" starburst senior-pm output/starburst output/starburst/content; '
                'cat "$prompt"',
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            "READ output/starburst/research_cache.json for company research",
            completed.stdout,
        )
        self.assertIn("role_research_cache.json", completed.stdout)
        self.assertIn(
            "Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py",
            completed.stdout,
        )
        self.assertIn(
            "Treat AGENTS.md as the navigation map, then read docs/resume-generation.md, docs/cover-letter-generation.md, docs/shared-inputs.md, and agent_preferences.md as needed.",
            completed.stdout,
        )
        self.assertIn(
            "When writing the cover letter body text, avoid the Unicode em dash character when possible.",
            completed.stdout,
        )
        self.assertIn(
            "Keep at least 6 bullets for Moody's, at least 5 bullets for Kyte, at least 3 bullets for T-Mobile, at least 1 bullet for Lyft, and at least 1 bullet for Allstate.",
            completed.stdout,
        )
        self.assertIn(
            "When the page budget is tight, prefer one concise accomplishment sentence per bullet for older roles instead of multi-clause bullets.",
            completed.stdout,
        )
        self.assertIn(
            "Prefer a compact 2-sentence summary over a longer 3-sentence summary when required bullets already make the resume dense.",
            completed.stdout,
        )

    def test_role_research_prompt_writes_to_role_cache(self):
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                "source scripts/llm_common.sh; prompt=$(mktemp); "
                'job_assets_write_role_research_prompt "$prompt" starburst senior-pm output/starburst output/starburst/content abc123hash; '
                'cat "$prompt"',
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("role_research_cache.json", completed.stdout)
        self.assertIn("abc123hash", completed.stdout)
        self.assertIn("Do NOT re-research company-level information", completed.stdout)

    def test_fix_prompt_forbids_recursive_repo_entrypoints(self):
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                "source scripts/llm_common.sh; prompt=$(mktemp); "
                "job_assets_write_fix_prompt \"$prompt\" output/starburst/content 'validation failed'; "
                'cat "$prompt"',
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            "Do NOT change the cover letter. Do NOT run build scripts or recurse into job-assets, apply.sh, scripts/run_pipeline.py, or scripts/job_assets_pipeline.py.",
            completed.stdout,
        )
        self.assertIn(
            "Do not reduce Moody's below 6 bullets, Kyte below 5 bullets, T-Mobile below 3 bullets, Lyft below 1 bullet, or Allstate below 1 bullet.",
            completed.stdout,
        )
        self.assertIn(
            "Rewrite older-role bullets down to one concise accomplishment sentence before you remove any required bullet.",
            completed.stdout,
        )
        self.assertIn(
            "Keep the summary to exactly 2 short sentences while fixing an overlong resume.",
            completed.stdout,
        )

    def test_capacity_error_helper_matches_quota_banner_with_unicode_punctuation(self):
        with TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "llm_drafting_raw.txt"
            log_path.write_text(
                "You're out of extra usage · resets Mar 28 at 11pm (America/Los_Angeles)\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "bash",
                    "-lc",
                    f"""
                    source scripts/llm_common.sh
                    job_assets_log_contains_provider_capacity_error "{log_path}"
                    """,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )

    def test_resolved_automation_provider_chain_filters_out_non_openai_gemini_entries(self):
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                """
                set -euo pipefail
                source scripts/llm_common.sh
                export ASSET_LLM_PROVIDER=openai
                export ASSET_LLM_PROVIDER_CHAIN=openai,gemini,claude,codex
                job_assets_resolve_provider_chain
                """,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(completed.stdout.strip(), "openai,gemini")

    def test_chain_fallback_advances_when_provider_hits_capacity_limit_without_writing_outputs(self):
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            fake_gemini = bin_dir / "gemini"
            fake_gemini.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_gemini.chmod(0o755)

            calls_path = tmp_path / "calls.txt"
            output_path = tmp_path / "resume_content.json"
            log_path = tmp_path / "llm_drafting_raw.txt"

            completed = subprocess.run(
                [
                    "bash",
                    "-lc",
                    f"""
                    set -euo pipefail
                    source scripts/llm_common.sh
                    export ASSET_LLM_PROVIDER_CHAIN=openai,gemini
                    export PATH="{bin_dir}:$PATH"
                    calls_path="{calls_path}"
                    output_path="{output_path}"
                    log_path="{log_path}"

                    job_assets_run_prompt() {{
                        local provider="$1"
                        local _prompt="$2"
                        local _mode="$3"
                        local current_log="$4"
                        echo "$provider" >> "$calls_path"
                        if [[ "$provider" == "openai" ]]; then
                            printf "You're out of extra usage · resets Mar 28 at 11pm (America/Los_Angeles)\\n" > "$current_log"
                            return 0
                        fi
                        printf '{{"provider":"%s"}}\\n' "$provider" > "$output_path"
                        printf "provider=%s\\n" "$provider" > "$current_log"
                        return 0
                    }}

                    job_assets_run_prompt_with_fallback chain /tmp/prompt draft "$log_path" "$output_path"
                    cat "$calls_path"
                    cat "$output_path"
                    """,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertEqual(calls_path.read_text(encoding="utf-8").splitlines(), ["openai", "gemini"])
            self.assertEqual(output_path.read_text(encoding="utf-8").strip(), '{"provider":"gemini"}')
            self.assertIn("gemini", completed.stdout)

    def test_single_provider_fallback_advances_when_primary_hits_capacity_limit_without_writing_outputs(self):
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            calls_path = tmp_path / "calls.txt"
            output_path = tmp_path / "resume_content.json"
            log_path = tmp_path / "llm_drafting_raw.txt"

            completed = subprocess.run(
                [
                    "bash",
                    "-lc",
                    f"""
                    set -euo pipefail
                    source scripts/llm_common.sh
                    export JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER=openai
                    calls_path="{calls_path}"
                    output_path="{output_path}"
                    log_path="{log_path}"

                    job_assets_run_prompt() {{
                        local provider="$1"
                        local _prompt="$2"
                        local _mode="$3"
                        local current_log="$4"
                        echo "$provider" >> "$calls_path"
                        if [[ "$provider" == "claude" ]]; then
                            printf "You've hit your usage limit. Upgrade to Pro.\\n" > "$current_log"
                            return 0
                        fi
                        printf '{{"provider":"%s"}}\\n' "$provider" > "$output_path"
                        printf "provider=%s\\n" "$provider" > "$current_log"
                        return 0
                    }}

                    job_assets_run_prompt_with_fallback claude /tmp/prompt draft "$log_path" "$output_path"
                    cat "$calls_path"
                    cat "$output_path"
                    """,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertEqual(calls_path.read_text(encoding="utf-8").splitlines(), ["claude", "openai"])
            self.assertEqual(output_path.read_text(encoding="utf-8").strip(), '{"provider":"openai"}')
            self.assertIn("openai", completed.stdout)
