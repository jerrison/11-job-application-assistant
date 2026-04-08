#!/usr/bin/env python3
"""Choose page_break_before after resume content is finalized.

This script updates resume_content.json in place so the page-break decision is
made after bullets and summary are settled, rather than being fixed in the
initial draft.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import json_lenient
from resume_layout import choose_page_break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optimize page_break_before in a resume_content.json file.",
    )
    parser.add_argument("input", help="Path to resume_content.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print the recommended break without modifying the file.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    with input_path.open() as fh:
        data = json_lenient.load(fh)

    previous = data.get("page_break_before")
    selected, diagnostics = choose_page_break(data)

    if selected:
        print(f"Recommended page_break_before: {selected}")
    else:
        print("Recommended page_break_before: (unchanged)")

    reason = diagnostics.get("reason")
    if reason:
        print(f"Reason: {reason}")

    selected_diag = diagnostics.get("selected")
    if selected_diag:
        print(
            "Layout estimate: "
            f"page1={selected_diag['page1_positions']} ({selected_diag['page1_height']}pt), "
            f"page2={selected_diag['page2_positions']} ({selected_diag['page2_height']}pt)"
        )

    if args.check:
        return

    if selected and selected != previous:
        data["page_break_before"] = selected
        with input_path.open("w") as fh:
            json.dump(data, fh, indent=4, ensure_ascii=False)
            fh.write("\n")
        print(f"Updated {input_path} ({previous!r} -> {selected!r})")
    else:
        print(f"No change needed for {input_path}")


if __name__ == "__main__":
    main()
