#!/usr/bin/env python3
"""Open a saved-portal browser profile for manual sign-in."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import saved_portal_import
from project_env import load_project_env

load_project_env()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "portal",
        choices=[spec.key for spec in saved_portal_import.list_saved_portals()],
        help="Saved portal to open for manual sign-in.",
    )
    args = parser.parse_args()

    saved_portal_import.launch_saved_portal_auth_setup(args.portal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
