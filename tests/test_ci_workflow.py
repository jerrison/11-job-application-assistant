import re
import tomllib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _generated_body(path: Path) -> str:
    """Strip the generated header and return the AGENTS.md body."""
    content = path.read_text(encoding="utf-8")
    return re.sub(r"\A(<!--.*?-->\n)+\n", "", content)


def _dependency_name(spec: str) -> str:
    base = re.split(r"[<>=!~ ]", str(spec), maxsplit=1)[0]
    return base.split("[", 1)[0]


class CiWorkflowTests(unittest.TestCase):
    def test_unit_tests_run_on_codex_pushes_and_main_pull_requests(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("push:", workflow)
        self.assertIn('- "codex/**"', workflow)
        self.assertIn("pull_request:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn("startsWith(github.head_ref, 'codex/')", workflow)
        self.assertIn("unit-tests:", workflow)

    def test_all_generated_provider_files_match_agents_md(self):
        """Every generated provider copy must mirror the canonical prompt."""
        agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        generated_files = [
            "CLAUDE.md",
            "GEMINI.md",
            "CODEX.md",
            "GPT.md",
            ".github/copilot-instructions.md",
        ]
        for rel_path in generated_files:
            with self.subTest(path=rel_path):
                body = _generated_body(PROJECT_ROOT / rel_path)
                self.assertEqual(
                    agents,
                    body,
                    f"AGENTS.md and {rel_path} have diverged. Run: uv run python scripts/sync_agent_files.py",
                )

    def test_web_extra_dependencies_are_available_in_default_dev_environment(self):
        """Web test modules are in the default suite, so the default dev env must include the web stack."""
        pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project_deps = pyproject.get("project", {}).get("dependencies", [])
        web_deps = pyproject.get("project", {}).get("optional-dependencies", {}).get("web", [])
        dev_deps = pyproject.get("dependency-groups", {}).get("dev", [])
        default_env_names = {_dependency_name(dep) for dep in (*project_deps, *dev_deps)}
        missing = sorted(_dependency_name(dep) for dep in web_deps if _dependency_name(dep) not in default_env_names)
        self.assertEqual(
            missing,
            [],
            "tests/test_job_web.py and tests/test_draft_web.py are part of the default test suite, "
            "so every dependency from [project.optional-dependencies].web must also be installed in the "
            "default dev environment. FIX: add the missing web dependencies to [dependency-groups].dev "
            f"or [project].dependencies. Missing: {', '.join(missing)}",
        )

    # -- Golden Principle structural tests (harness engineering) -----------

    def test_agents_md_is_concise_map(self):
        """Golden Principle: AGENTS.md is a ~100-line map, not an encyclopedia.

        See https://openai.com/index/harness-engineering/ — 'Instead of treating
        AGENTS.md as the encyclopedia, we treat it as the table of contents.'
        """
        agents_md = (PROJECT_ROOT / "AGENTS.md").read_text()
        line_count = len(agents_md.splitlines())
        self.assertLessEqual(
            line_count,
            110,
            f"AGENTS.md is {line_count} lines (max 110). "
            f"FIX: Move detailed instructions to docs/ and add a pointer in AGENTS.md. "
            f"Resume workflow → docs/resume-generation.md, "
            f"Cover letter workflow → docs/cover-letter-generation.md.",
        )

    def test_agents_md_doc_pointers_are_valid(self):
        """Golden Principle: doc pointers must resolve to real files.

        Prevents documentation rot by mechanically validating cross-links.
        """
        import re

        agents_md = (PROJECT_ROOT / "AGENTS.md").read_text()
        # Find all docs/ references like docs/resume-generation.md
        doc_refs = re.findall(r"docs/[\w-]+\.md", agents_md)
        self.assertGreater(len(doc_refs), 0, "AGENTS.md should contain docs/ pointers")
        for ref in doc_refs:
            path = PROJECT_ROOT / ref
            self.assertTrue(
                path.exists(),
                f"AGENTS.md references '{ref}' but the file does not exist. "
                f"FIX: Create {ref} or update the pointer in AGENTS.md.",
            )

    def test_claude_md_doc_pointers_are_valid(self):
        """Golden Principle: doc pointers must resolve to real files."""
        import re

        claude_md = (PROJECT_ROOT / "CLAUDE.md").read_text()
        doc_refs = re.findall(r"docs/[\w-]+\.md", claude_md)
        for ref in doc_refs:
            path = PROJECT_ROOT / ref
            self.assertTrue(
                path.exists(),
                f"CLAUDE.md references '{ref}' but the file does not exist. "
                f"FIX: Create {ref} or update the pointer in CLAUDE.md.",
            )

    def test_no_script_exceeds_line_limit(self):
        """Golden Principle: file size limits keep code agent-legible.

        Large files exceed agent context windows and make reasoning unreliable.
        """
        max_lines = 2500
        # Known exceptions — tracked for consolidation, not exempt from scrutiny
        known_large = {
            "application_submit_common.py": 8746,  # Baseline is now 8746 lines; shared submit orchestration absorbed proof, policy, retry, deterministic-answer handling, queue-proof backfills, answer-state sync hooks, recent resume-experience boundary fixes, and packaged-runtime path helpers before the planned module split lands
            "autofill_common.py": 2988,  # Baseline is now 2988 lines; cross-board answer classification, option-normalization helpers, proof cleanup, and stale-artifact handling broadened the autofill hub before its planned split
            "autofill_greenhouse.py": 9611,  # Baseline is now 9611 lines; resume attachment recovery, confirmation reply plumbing, review-proof handling, and packaged-runtime path/display guards broadened the Greenhouse runner while the tracked module split lands
            "autofill_icims.py": 2646,  # Baseline is now 2646 lines; auth detection, blocker handling, and proof capture broadened the ICIMS runner before its planned split
            "autofill_linkedin.py": 2748,  # Baseline is now 2748 lines; resume verification, modal-proof handling, single-step submit safeguards, and extracted runtime-path support broadened the LinkedIn runner before its planned split
            "autofill_phenom.py": 2879,  # Baseline is now 2879 lines; cross-board proof, deterministic-answer logic, hybrid-location handling, and packaged-runtime db/path guards broadened the Phenom runner before its planned split
            "autofill_workday.py": 6785,  # Baseline is now 6785 lines; deterministic question filling, prompt/checkbox recovery, review-boundary guards, recent answer-proof handling, and packaged-runtime display-path guards broadened the Workday hub before the planned module split
            "job_db.py": 3909,  # Baseline is now 3909 lines; submission locks, repair clustering, disk sync, duplicate normalization, current-attempt sync state, and extracted repo/runtime separation broadened the jobs DB hub before the planned persistence split lands
            "pipeline_orchestrator.py": 3700,  # Baseline is now 3700 lines; draft audit, captcha escalation, repair runtime hooks, provider fallback plumbing, and packaged-runtime output-root discovery broadened the runtime hub before the tracked split lands
        }
        scripts_dir = PROJECT_ROOT / "scripts"
        for py_file in scripts_dir.glob("*.py"):
            line_count = len(py_file.read_text().splitlines())
            limit = known_large.get(py_file.name, max_lines)
            self.assertLessEqual(
                line_count,
                limit,
                f"{py_file.name} is {line_count} lines (max {limit}). "
                f"FIX: Split into focused modules. Extract shared utilities to "
                f"a separate file and import them.",
            )

    def test_operational_rules_shared_across_providers(self):
        """Golden Principle: operational rules are LLM-agnostic, shared by all providers."""
        rules_path = PROJECT_ROOT / "docs" / "operational-rules.md"
        self.assertTrue(
            rules_path.exists(),
            "FIX: Create docs/operational-rules.md with standing orders and post-fix workflow.",
        )
        agents_md = (PROJECT_ROOT / "AGENTS.md").read_text()
        claude_md = (PROJECT_ROOT / "CLAUDE.md").read_text()
        self.assertIn(
            "operational-rules.md",
            agents_md,
            "FIX: Add pointer to docs/operational-rules.md in AGENTS.md.",
        )
        self.assertIn(
            "operational-rules.md",
            claude_md,
            "FIX: Add pointer to docs/operational-rules.md in CLAUDE.md.",
        )

    def test_backlog_sweep_contract_is_documented_and_checkable(self):
        """Large queue sweeps must have a machine-checkable completion gate."""
        backlog_doc = PROJECT_ROOT / "docs" / "backlog-sweep.md"
        checker = PROJECT_ROOT / "scripts" / "check_backlog_sweep.py"
        self.assertTrue(
            backlog_doc.exists(),
            "FIX: Create docs/backlog-sweep.md with the snapshot, ledger, and completion-gate contract.",
        )
        self.assertTrue(
            checker.exists(),
            "FIX: Add scripts/check_backlog_sweep.py so backlog completion is mechanically enforceable.",
        )

        agents_md = (PROJECT_ROOT / "AGENTS.md").read_text()
        index_md = (PROJECT_ROOT / "docs" / "INDEX.md").read_text()
        backlog_text = backlog_doc.read_text()

        self.assertIn(
            "backlog-sweep.md",
            agents_md,
            "FIX: Add a compact AGENTS.md pointer to docs/backlog-sweep.md.",
        )
        self.assertIn(
            "backlog-sweep.md",
            index_md,
            "FIX: Add docs/backlog-sweep.md to docs/INDEX.md so future agents can discover it.",
        )
        self.assertIn(
            "check_backlog_sweep.py",
            backlog_text,
            "FIX: Document the completion-gate command in docs/backlog-sweep.md.",
        )

    def test_exec_plan_scaffolding_exists(self):
        """Harness-style execution plan scaffolding must exist in the repo."""
        required_paths = [
            PROJECT_ROOT / "docs" / "exec-plans" / "README.md",
            PROJECT_ROOT / "docs" / "exec-plans" / "active",
            PROJECT_ROOT / "docs" / "exec-plans" / "completed",
            PROJECT_ROOT / "docs" / "PLAN_TEMPLATE.md",
        ]
        for path in required_paths:
            with self.subTest(path=path):
                self.assertTrue(
                    path.exists(),
                    f"Missing execution-plan path: {path.relative_to(PROJECT_ROOT)}",
                )

        plan_template = (PROJECT_ROOT / "docs" / "PLAN_TEMPLATE.md").read_text(encoding="utf-8")
        for section in (
            "## Purpose / Big Picture",
            "## Context and Orientation",
            "## Milestones",
            "## Progress",
            "## Surprises & Discoveries",
            "## Decision Log",
            "## Outcomes & Retrospective",
        ):
            with self.subTest(section=section):
                self.assertIn(
                    section,
                    plan_template,
                    f"PLAN_TEMPLATE.md missing required section: {section}",
                )


if __name__ == "__main__":
    unittest.main()
