#!/usr/bin/env python3
"""Validate architectural import constraints.

Enforces dependency directions in the autofill composition architecture:
  autofill_common.py  →  autofill_pipeline.py  →  autofill_{board}.py
  application_submit_common.py  →  submit_application.py

Rules:
  ARCH001 — Cross-layer violation: infra module imports from a board module
  ARCH002 — Cross-board violation: board module imports from another board module

Run: uv run python scripts/check_architecture.py
CI:  This script runs in the 'architecture' job and fails on violations.
"""

import ast
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

INFRA_MODULES = {"autofill_common", "autofill_pipeline"}
BOARD_MODULES = {f.stem for f in SCRIPTS_DIR.glob("autofill_*.py") if f.stem not in INFRA_MODULES}

# Layer rules: module -> set of modules it must NOT import from
FORBIDDEN_IMPORTS: dict[str, set[str]] = {
    # Common utilities must not depend on pipeline or board-specific code
    "autofill_common": BOARD_MODULES | {"autofill_pipeline"},
    # Pipeline orchestration must not depend on board-specific code
    "autofill_pipeline": BOARD_MODULES,
    # Lower-level submit common must not depend on board-specific code
    "application_submit_common": BOARD_MODULES,
}

# Board scripts must not import from each other
for board in BOARD_MODULES:
    FORBIDDEN_IMPORTS[board] = BOARD_MODULES - {board}


def get_imports(filepath: Path) -> list[tuple[str, int]]:
    """Extract imported module names and their line numbers from a Python file."""
    try:
        tree = ast.parse(filepath.read_text(), filename=str(filepath))
    except SyntaxError:
        return []

    def root_module_name(name: str) -> str:
        parts = name.split(".")
        if len(parts) > 1 and parts[0] == "scripts":
            return parts[1]
        return parts[0]

    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((root_module_name(alias.name), node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((root_module_name(node.module), node.lineno))
    return imports


def _rule_code(module_name: str, imported: str) -> str:
    """Return ARCH001 for cross-layer or ARCH002 for cross-board violations."""
    if module_name in BOARD_MODULES and imported in BOARD_MODULES:
        return "ARCH002"
    return "ARCH001"


def check_architecture() -> list[str]:
    """Check all import rules and return list of violations."""
    violations = []

    for module_name, forbidden in FORBIDDEN_IMPORTS.items():
        filepath = SCRIPTS_DIR / f"{module_name}.py"
        if not filepath.exists():
            continue

        imports = get_imports(filepath)
        # Build set of forbidden imports found, with line numbers
        found: dict[str, int] = {}
        for imp, lineno in imports:
            if imp in forbidden and imp not in found:
                found[imp] = lineno

        for imp in sorted(found):
            lineno = found[imp]
            code = _rule_code(module_name, imp)
            violations.append(
                f"scripts/{module_name}.py:{lineno}: {code}: "
                f"{module_name} imports {imp} "
                f"— board scripts must not cross-import. "
                f"Move shared code to autofill_common.py (browser utils), "
                f"application_submit_common.py (submit logic), "
                f"or question_classifier.py (classification)."
            )

    return violations


def main() -> int:
    violations = check_architecture()

    if violations:
        print(f"Architecture validation FAILED — {len(violations)} violation(s):\n")
        for v in violations:
            print(f"  {v}")
        return 1

    print("Architecture validation passed — no import direction violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
