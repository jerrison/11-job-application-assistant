import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "codex-pr.yml"


class CodexPrWorkflowTests(unittest.TestCase):
    def test_auto_merge_uses_configured_merge_method_flag(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("gh pr merge", workflow)
        self.assertIn("--auto", workflow)
        self.assertIn('method="${CODEX_PR_MERGE_METHOD:-squash}"', workflow)
        self.assertIn('merge_flag="--merge"', workflow)
        self.assertIn('merge_flag="--squash"', workflow)
        self.assertIn('merge_flag="--rebase"', workflow)
        self.assertIn('"${merge_flag}"', workflow)


if __name__ == "__main__":
    unittest.main()
