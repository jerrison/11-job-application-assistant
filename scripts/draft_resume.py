#!/usr/bin/env python3
"""Draft resume content from ranked bullets.

Takes the output of rank_bullets.py (scored bullets per company) and generates
a draft resume_content.json that the LLM can then review and edit.

Usage:
    uv run scripts/draft_resume.py output/samsara/principal-pm-maintenance/content/ranked_bullets.json \
        -o output/samsara/principal-pm-maintenance/content/resume_content_draft.json
"""

import argparse
import json
import os
import re

# ─── Selection heuristics per company ────────────────────────────────────────
# (max_bullets, min_bullets)
SELECTION_RULES = {
    "moodys": (7, 6),
    "kyte": (6, 5),
    "tmobile": (3, 3),
    "lyft": (1, 1),
    "allstate": (1, 1),
}

POSITION_ORDER = ["moodys", "kyte", "tmobile", "lyft", "allstate"]

SCORE_FLOOR = 0.05  # Exclude bullets below this score unless at minimum

MASTER_TAGLINE = "Principal Product Manager  |  AI/ML & Enterprise B2B  |  Wharton MBA + Penn M.S. Computer Science"

DEFAULT_PAGE_BREAK = None


# ─── Bold/text splitting ────────────────────────────────────────────────────


def split_bold_text(bullet_text: str) -> dict:
    """Split a bullet into {"bold": "...", "text": "..."}.

    Heuristics (in priority order):
    1. Find the first comma or period that comes after a number/percentage,
       and use everything up to and including that punctuation + space as bold.
    2. Find a participle/transition phrase (by, enabling, transforming, etc.)
       and split before it.
    3. Fallback: first sentence fragment up to the first comma.
    """
    text = bullet_text.strip()

    # Heuristic 1: comma or period after a number/percentage/dollar amount
    # Look for patterns like "166% YoY client growth," or "$8M TCV)"
    # We want the first comma/period that appears after a number-like token
    pattern_after_number = re.compile(
        r"(.*?\d[\d,.]*[%KMB]?(?:\s+\w+){0,6}?[),.])\s+(.*)",
        re.DOTALL,
    )
    m = pattern_after_number.match(text)
    if m:
        bold_part = m.group(1).rstrip()
        rest = m.group(2)
        # Sanity: bold shouldn't be more than ~60% of the text
        if len(bold_part) < len(text) * 0.65 and len(bold_part) > 10:
            return {"bold": bold_part + " ", "text": rest}

    # Heuristic 2: split before participial/transition phrases
    transition_pattern = re.compile(
        r"^(.*?[,.])\s+"
        r"((?:by|enabling|transforming|achieving|unlocking|reducing|generating|"
        r"resulting|leading|expanding|creating|driving|increasing|establishing|"
        r"improving|launching|building|designing|partnering|delivering|"
        r"eliminating|automating|accelerating|balancing|ensuring|integrating|"
        r"performing|shortening|spanning|coordinating|streamlining)\s+.*)",
        re.IGNORECASE | re.DOTALL,
    )
    m = transition_pattern.match(text)
    if m:
        bold_part = m.group(1).rstrip()
        rest = m.group(2)
        if len(bold_part) < len(text) * 0.65 and len(bold_part) > 10:
            return {"bold": bold_part + " ", "text": rest}

    # Heuristic 3: fallback — first comma-separated fragment
    comma_idx = text.find(",")
    if comma_idx > 10 and comma_idx < len(text) * 0.65:
        return {"bold": text[: comma_idx + 1] + " ", "text": text[comma_idx + 2 :]}

    # Last resort: first period-separated fragment
    period_idx = text.find(".")
    if period_idx > 10 and period_idx < len(text) - 5:
        return {"bold": text[: period_idx + 1] + " ", "text": text[period_idx + 2 :]}

    # Can't split meaningfully — put it all in bold
    return {"bold": text + " ", "text": ""}


# ─── Main logic ─────────────────────────────────────────────────────────────


def select_bullets(ranked_data: dict) -> tuple[dict, dict]:
    """Select bullets per company based on score and heuristics.

    Returns (selected, excluded) dicts mapping company -> list of bullet texts.
    """
    selected = {}
    excluded = {}

    for company in POSITION_ORDER:
        if company not in ranked_data:
            selected[company] = []
            excluded[company] = []
            continue

        bullets_with_scores = ranked_data[company]

        # Sort by score descending
        sorted_bullets = sorted(bullets_with_scores, key=lambda b: b["score"], reverse=True)

        max_count, min_count = SELECTION_RULES.get(company, (2, 1))

        chosen = []
        cut = []

        for _i, item in enumerate(sorted_bullets):
            bullet_text = item["bullet"]
            score = item["score"]

            if len(chosen) < min_count:
                # Must include to meet minimum
                chosen.append(bullet_text)
            elif len(chosen) < max_count and score >= SCORE_FLOOR:
                chosen.append(bullet_text)
            else:
                cut.append(bullet_text)

        selected[company] = chosen
        excluded[company] = cut

    return selected, excluded


def build_draft(selected: dict, excluded: dict) -> dict:
    """Build the draft resume_content JSON structure."""
    positions = {}
    for company in POSITION_ORDER:
        bullets = selected.get(company, [])
        positions[company] = [split_bold_text(b) for b in bullets]

    # Only include companies with excluded bullets in _meta
    bullets_excluded = {}
    for company in POSITION_ORDER:
        cut = excluded.get(company, [])
        if cut:
            bullets_excluded[company] = cut

    return {
        "tagline": MASTER_TAGLINE,
        "summary": None,
        "positions": positions,
        "page_break_before": DEFAULT_PAGE_BREAK,
        "_meta": {
            "note": (
                "DRAFT - Review and edit. Summary needs to be written by LLM. "
                "Bullet text may need rewriting for the target role. "
                "Determine page_break_before only after the final bullets and summary are set."
            ),
            "bullets_excluded": bullets_excluded,
        },
    }


def print_summary(selected: dict, excluded: dict):
    """Print a human-readable summary of what was selected and cut."""
    print("\n" + "=" * 70)
    print("DRAFT RESUME BULLET SELECTION SUMMARY")
    print("=" * 70)

    total_selected = 0
    total_excluded = 0

    for company in POSITION_ORDER:
        chosen = selected.get(company, [])
        cut = excluded.get(company, [])
        total_selected += len(chosen)
        total_excluded += len(cut)

        max_count, min_count = SELECTION_RULES.get(company, (2, 1))
        print(f"\n{company.upper()} — {len(chosen)} selected (range: {min_count}-{max_count})")

        for i, b in enumerate(chosen, 1):
            # Truncate for display
            display = b[:80] + "..." if len(b) > 80 else b
            print(f"  [{i}] {display}")

        if cut:
            print(f"  --- {len(cut)} excluded ---")
            for b in cut:
                display = b[:80] + "..." if len(b) > 80 else b
                print(f"  [x] {display}")

    print(f"\n{'=' * 70}")
    print(f"Total: {total_selected} selected, {total_excluded} excluded")
    print("Summary: null (LLM to write)")
    print("Page break before: (set later after content is finalized)")
    print(f"{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description="Draft resume_content.json from ranked bullets")
    parser.add_argument(
        "input",
        help="Path to ranked_bullets.json (output of rank_bullets.py)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output/resume_content_draft.json",
        help="Output path for draft resume_content.json",
    )
    args = parser.parse_args()

    # Read ranked bullets
    with open(args.input) as f:
        ranked_data = json.load(f)

    # Select bullets — ranked data has positions nested under "positions" key
    positions_data = ranked_data.get("positions", ranked_data)
    selected, excluded = select_bullets(positions_data)

    # Build draft JSON
    draft = build_draft(selected, excluded)

    # Write output
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(draft, f, indent=4, ensure_ascii=False)

    print(f"Draft resume content written to: {args.output}")

    # Print summary
    print_summary(selected, excluded)


if __name__ == "__main__":
    main()
