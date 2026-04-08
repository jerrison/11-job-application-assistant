"""Lightweight resume layout heuristics for page-break selection."""

from __future__ import annotations

POSITION_ORDER = ["moodys", "kyte", "tmobile", "lyft", "allstate"]

POSITIONS_META = {
    "moodys": {
        "company": "MOODY'S ANALYTICS",
        "title": "Associate Director, Product Management",
        "dates": "San Francisco, CA | 2024\u2013Present",
    },
    "kyte": {
        "company": "KYTE",
        "title": "Staff Product Manager",
        "subtitle": " (Series B, On-Demand Car Rental)",
        "dates": "San Francisco, CA | 2022\u20132024 | 150 employees, 15K+ monthly transactions",
    },
    "tmobile": {
        "company": "T-MOBILE",
        "title": "Senior Product Manager, IoT Platform",
        "dates": "New York, NY | 2020\u20132022 | Enterprise IoT Business Unit",
    },
    "lyft": {
        "company": "LYFT",
        "title": "Senior Actuarial Analyst (First Actuary Hire)",
        "dates": "San Francisco, CA | 2016\u20132017",
    },
    "allstate": {
        "company": "ALLSTATE",
        "title": "Senior Actuarial Analyst",
        "dates": "San Francisco, CA | 2013\u20132016",
    },
}

EDUCATION = [
    {
        "school": "THE WHARTON SCHOOL, UNIVERSITY OF PENNSYLVANIA",
        "degree": "MBA (Finance)",
        "location": "Philadelphia, PA | 2018\u20132020",
        "details": [
            "Joseph Wharton Fellow (academic, personal, and professional achievement)",
            "GMAT 750 (98th percentile)",
        ],
    },
    {
        "school": "PENN ENGINEERING, UNIVERSITY OF PENNSYLVANIA",
        "degree": "M.S. Computer Science",
        "location": "Philadelphia, PA | 2018\u20132020",
        "details": [
            "Ripple Research Fellow (blockchain applications in P&C insurance). Relevant coursework: Applied ML, Database Systems, AI.",
        ],
    },
    {
        "school": "FLORIDA STATE UNIVERSITY",
        "degree": "B.S. Actuarial Science & Computational Science (Dual Degree)",
        "location": "Tallahassee, FL | 2009\u20132013",
        "details": ["Phi Beta Kappa, Magna Cum Laude"],
    },
]

SKILLS = [
    ("Competitions:", "Gold Medalist, National Physics Olympiad, Panama (3,500 students)"),
    (
        "Technical:",
        "Python, SQL, TypeScript, Figma | ML/AI: Snowflake Cortex, LLM orchestration, RAG systems, GLMs | Data: A/B testing, analytics pipelines",
    ),
    ("Languages:", "Spanish (native), Cantonese (native), Mandarin (advanced)"),
    ("Certifications:", "Associate of the Casualty Actuarial Society (ACAS)"),
    ("Work Authorization:", "United States Citizen"),
]

USABLE_PAGE_HEIGHT_PT = 720
PAGE_BREAK_SAFETY_BUFFER_PT = 12
TITLE_CHARS_PER_LINE = 84
SUMMARY_CHARS_PER_LINE = 104
BULLET_CHARS_PER_LINE = 92
DETAIL_CHARS_PER_LINE = 98
SKILL_CHARS_PER_LINE = 96


def _estimate_wrapped_lines(text: str, chars_per_line: int) -> int:
    if not text:
        return 0

    text = text.replace("\u2014", "-")
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    if not paragraphs:
        return 0

    lines = 0
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            continue

        current = 0
        paragraph_lines = 1
        for word in words:
            word_len = len(word)
            if current == 0:
                current = word_len
                continue
            if current + 1 + word_len <= chars_per_line:
                current += 1 + word_len
            else:
                paragraph_lines += 1
                current = word_len
        lines += paragraph_lines

    return max(lines, 1)


def _estimate_position_height(meta: dict, bullets: list) -> float:
    title_text = meta["company"] + " - " + meta["title"] + meta.get("subtitle", "")
    title_lines = _estimate_wrapped_lines(title_text, TITLE_CHARS_PER_LINE)
    dates_lines = _estimate_wrapped_lines(meta["dates"], TITLE_CHARS_PER_LINE)

    height = 5 + (12 * title_lines)
    height += 4 + (10 * dates_lines)

    for bullet in bullets:
        if isinstance(bullet, dict):
            bullet_text = (bullet.get("bold", "") + bullet.get("text", "")).strip()
        else:
            bullet_text = str(bullet).strip()
        bullet_lines = _estimate_wrapped_lines(bullet_text, BULLET_CHARS_PER_LINE)
        height += 3 + (11 * bullet_lines)

    return height


def _estimate_summary_height(summary: str | None) -> float:
    if not summary:
        return 0
    summary_lines = _estimate_wrapped_lines(summary, SUMMARY_CHARS_PER_LINE)
    return 30 + 6 + (11 * summary_lines)


def _estimate_static_page1_height(data: dict) -> float:
    return 25 + 15 + 20 + _estimate_summary_height(data.get("summary")) + 30


def _estimate_education_skills_height() -> float:
    height = 30

    for edu in EDUCATION:
        school_lines = _estimate_wrapped_lines(edu["school"], TITLE_CHARS_PER_LINE)
        degree_text = edu["degree"] + " | " + edu["location"]
        degree_lines = _estimate_wrapped_lines(degree_text, DETAIL_CHARS_PER_LINE)
        height += 2 + (11 * school_lines)
        height += 2 + (11 * degree_lines)
        for detail in edu["details"]:
            detail_lines = _estimate_wrapped_lines(detail, DETAIL_CHARS_PER_LINE)
            height += 5 + (10 * detail_lines)

    height += 30
    for label, value in SKILLS:
        skill_lines = _estimate_wrapped_lines(label + " " + value, SKILL_CHARS_PER_LINE)
        height += 3 + (10.5 * skill_lines)

    return height


def choose_page_break(data: dict) -> tuple[str | None, dict]:
    included_positions = [pos_id for pos_id in POSITION_ORDER if data.get("positions", {}).get(pos_id)]

    diagnostics = {
        "included_positions": included_positions,
        "candidates": [],
    }

    if len(included_positions) <= 1:
        diagnostics["reason"] = "Only one populated position block."
        return data.get("page_break_before"), diagnostics

    static_page1 = _estimate_static_page1_height(data)
    static_page2 = _estimate_education_skills_height()
    position_heights = {
        pos_id: _estimate_position_height(POSITIONS_META[pos_id], data["positions"][pos_id])
        for pos_id in included_positions
    }

    best_candidate = None
    overflow_candidate = None

    for index, candidate in enumerate(included_positions[1:], start=1):
        page1_ids = included_positions[:index]
        page2_ids = included_positions[index:]
        page1_height = static_page1 + sum(position_heights[pos_id] for pos_id in page1_ids)
        page2_height = static_page2 + sum(position_heights[pos_id] for pos_id in page2_ids)

        candidate_diag = {
            "page_break_before": candidate,
            "page1_positions": page1_ids,
            "page2_positions": page2_ids,
            "page1_height": round(page1_height, 1),
            "page2_height": round(page2_height, 1),
        }
        diagnostics["candidates"].append(candidate_diag)

        if page1_height <= (USABLE_PAGE_HEIGHT_PT - PAGE_BREAK_SAFETY_BUFFER_PT):
            best_candidate = candidate_diag
        elif overflow_candidate is None:
            overflow_candidate = candidate_diag

    if best_candidate is not None:
        diagnostics["selected"] = best_candidate
        diagnostics["reason"] = "Latest page break that keeps page 1 comfortably within one page."
        return best_candidate["page_break_before"], diagnostics

    diagnostics["selected"] = overflow_candidate
    diagnostics["reason"] = "No candidate kept page 1 within one page estimate; chose earliest break."
    return overflow_candidate["page_break_before"], diagnostics
