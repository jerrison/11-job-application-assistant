#!/usr/bin/env python3
"""Parse raw job description text into structured JSON.

Usage:
    uv run scripts/parse_jd.py input.txt -o output/samsara/principal-pm-maintenance/content/jd_parsed.json
    uv run scripts/scrape_job.py "https://..." | uv run scripts/parse_jd.py - -o output/samsara/principal-pm-maintenance/content/jd_parsed.json
    cat jd.txt | uv run scripts/parse_jd.py -o output.json

Handles both plain-text JDs and Greenhouse API JSON format.
No LLM calls — fully deterministic heuristic parsing.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants / keyword banks
# ---------------------------------------------------------------------------

LEVEL_KEYWORDS: dict[str, list[str]] = {
    "principal": ["principal"],
    "staff": ["staff"],
    "senior": ["senior", "sr.", "sr "],
    "lead": ["lead"],
    "director": ["director"],
    "vp": ["vp", "vice president"],
    "mid": ["mid-level", "mid level", "ii", "iii"],
    "entry": ["entry", "junior", "jr.", "jr ", "associate", "new grad", "i"],
}

# Canonical ordering for level resolution (highest wins)
LEVEL_PRIORITY = ["vp", "director", "principal", "staff", "lead", "senior", "mid", "entry"]

# Section header patterns (lowercase) mapped to canonical section names
SECTION_PATTERNS: list[tuple[str, str]] = [
    # ORDER MATTERS — more specific patterns MUST come before generic ones.
    # "minimum requirements for the role" must match required_qualifications,
    # not responsibilities via "the role".
    # preferred qualifications — most specific first
    (r"preferred qualifications", "preferred_qualifications"),
    (r"preferred requirements", "preferred_qualifications"),
    (r"nice[- ]to[- ]haves?", "preferred_qualifications"),
    (r"nice to have", "preferred_qualifications"),
    (r"bonus points", "preferred_qualifications"),
    (r"ideally,?\s*you", "preferred_qualifications"),
    (r"it'?s? a plus if", "preferred_qualifications"),
    (r"an ideal candidate", "preferred_qualifications"),
    (r"ideal qualifications", "preferred_qualifications"),
    (r"additionally", "preferred_qualifications"),
    (r"preferred", "preferred_qualifications"),
    (r"desired", "preferred_qualifications"),
    (r"bonus", "preferred_qualifications"),
    (r"plus if you", "preferred_qualifications"),
    # required qualifications — before responsibilities to avoid "requirements" matching "the role"
    (r"minimum qualifications", "required_qualifications"),
    (r"minimum requirements", "required_qualifications"),
    (r"basic qualifications", "required_qualifications"),
    (r"required qualifications", "required_qualifications"),
    (r"(?:some of the )?things we look for", "required_qualifications"),
    (r"what we'?re looking for", "required_qualifications"),
    (r"what you'?ll? need", "required_qualifications"),
    (r"what you bring", "required_qualifications"),
    (r"must[- ]have(?: experience| qualifications| requirements)?", "required_qualifications"),
    (r"must haves?", "required_qualifications"),
    (r"must have", "required_qualifications"),
    (r"your experience", "required_qualifications"),
    (r"requirements", "required_qualifications"),
    (r"qualifications", "required_qualifications"),
    (r"who you are", "required_qualifications"),
    (r"you have", "required_qualifications"),
    # responsibilities — last, since patterns like "the role" are very generic
    (r"what you'?ll do", "responsibilities"),
    (r"what you will do", "responsibilities"),
    (r"in this role,?\s*you will", "responsibilities"),
    (r"key responsibilities", "responsibilities"),
    (r"responsibilities", "responsibilities"),
    (r"your impact", "responsibilities"),
    (r"what you'?ll work on", "responsibilities"),
    (r"day.to.day", "responsibilities"),
    (r"role description", "responsibilities"),
    (r"job description", "responsibilities"),
    (r"about the role", "responsibilities"),
    (r"the role", "responsibilities"),
]

HEADER_NORMALIZATION_TRANSLATION = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "–": "-",
        "—": "-",
        "‑": "-",
        "‐": "-",
    }
)

GENERIC_COMPANY_NAME_VALUES = {
    "about the role",
    "about the team",
    "career opportunities",
    "careers",
    "careers listing",
    "job description",
    "our mission",
    "our team",
    "position overview",
    "search for jobs",
    "the company",
    "the opportunity",
    "the position",
    "the role",
    "the team",
    "this opportunity",
    "this position",
    "this role",
}

GENERIC_COMPANY_NAME_SUFFIXES = (
    " job board",
)

LOCATIONISH_COMPANY_NAME_VALUES = {
    "hybrid",
    "on site",
    "onsite",
    "remote",
}

GENERIC_JOB_TITLE_VALUES = {
    "career opportunities",
    "careers",
    "careers listing",
    "job application",
    "job applications",
    "job opening",
    "job openings",
    "jobs",
    "open positions",
    "open roles",
    "notion",
}


def _normalize_company_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _strip_generic_company_suffix(value: str | None) -> str:
    """Drop wrapper suffixes like "Job Board" and keep the employer stem."""
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    for suffix in GENERIC_COMPANY_NAME_SUFFIXES:
        suffix_words = suffix.strip()
        if not suffix_words:
            continue
        trimmed = re.sub(rf"(?i)\b{re.escape(suffix_words)}\s*$", "", candidate).strip(" -|,")
        if trimmed != candidate and trimmed:
            return trimmed
    return candidate


def company_name_looks_generic(value: str | None) -> bool:
    """Return True when a candidate looks like wrapper copy, not an employer."""
    normalized = _normalize_company_name(value)
    if not normalized:
        return False
    return normalized in GENERIC_COMPANY_NAME_VALUES or any(
        normalized.endswith(suffix) for suffix in GENERIC_COMPANY_NAME_SUFFIXES
    )


def company_name_looks_locationish(value: str | None) -> bool:
    """Return True when a candidate reads like a work-arrangement label."""
    normalized = _normalize_company_name(value)
    if not normalized:
        return False
    return normalized in LOCATIONISH_COMPANY_NAME_VALUES


def _normalize_title_candidate(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def title_looks_generic(value: str | None) -> bool:
    """Return True when a candidate is wrapper copy instead of a role title."""
    normalized = _normalize_title_candidate(value)
    if not normalized:
        return False
    return normalized in GENERIC_JOB_TITLE_VALUES or normalized.startswith("careers at ")


# Well-known technology / methodology keywords to look for
TECH_KEYWORDS: set[str] = {
    # Languages & runtimes
    "Python",
    "Java",
    "JavaScript",
    "TypeScript",
    "Go",
    "Golang",
    "Rust",
    "C++",
    "C#",
    "Ruby",
    "Scala",
    "Kotlin",
    "Swift",
    "R",
    "PHP",
    "Perl",
    "SQL",
    "NoSQL",
    "GraphQL",
    # Frameworks & libraries
    "React",
    "Angular",
    "Vue",
    "Node.js",
    "Django",
    "Flask",
    "FastAPI",
    "Spring",
    "Rails",
    "Next.js",
    ".NET",
    "TensorFlow",
    "PyTorch",
    "Keras",
    "Pandas",
    "NumPy",
    "Spark",
    "Hadoop",
    "Kafka",
    "Airflow",
    "dbt",
    # Cloud / infra
    "AWS",
    "GCP",
    "Azure",
    "Docker",
    "Kubernetes",
    "K8s",
    "Terraform",
    "CI/CD",
    "Jenkins",
    "GitHub Actions",
    "CircleCI",
    "Datadog",
    "Splunk",
    "Snowflake",
    "BigQuery",
    "Redshift",
    "Databricks",
    "Looker",
    "Tableau",
    "Amplitude",
    "Mixpanel",
    "Segment",
    # Databases
    "PostgreSQL",
    "MySQL",
    "MongoDB",
    "Redis",
    "DynamoDB",
    "Cassandra",
    "Elasticsearch",
    "Pinecone",
    "Qdrant",
    # AI/ML
    "LLM",
    "NLP",
    "ML",
    "AI",
    "Machine Learning",
    "Deep Learning",
    "Computer Vision",
    "RAG",
    "GPT",
    "Generative AI",
    "Gen AI",
    "Reinforcement Learning",
    "Fine-tuning",
    # PM / methodologies
    "Agile",
    "Scrum",
    "Kanban",
    "OKR",
    "OKRs",
    "KPI",
    "KPIs",
    "A/B Testing",
    "PRD",
    "PRDs",
    "Roadmap",
    "Sprint",
    "Jira",
    "Confluence",
    "Figma",
    "Notion",
    "Linear",
    "Asana",
    # Domains
    "IoT",
    "SaaS",
    "B2B",
    "B2C",
    "API",
    "APIs",
    "REST",
    "gRPC",
    "Microservices",
    "ETL",
    "ELT",
    "Data Pipeline",
    "Data Warehouse",
    "Marketplace",
    "Platform",
    "SDK",
    "CLI",
    "GDPR",
    "SOC 2",
    "HIPAA",
    "FedRAMP",
    "PCI",
}

# Patterns that signal various role attributes
SIGNAL_PATTERNS: dict[str, list[str]] = {
    "ic_role": [
        r"individual contributor",
        r"\bIC\b",
        r"hands.on",
        r"write code",
        r"build.*features",
        r"design.*systems",
        r"own the product",
        r"product manager",
        r"product management",
        r"software engineer",
        r"data scientist",
        r"analyst",
    ],
    "management": [
        r"manage a team",
        r"people manager",
        r"direct reports",
        r"leadership role",
        r"manage.*engineers",
        r"manage.*team",
        r"hiring",
        r"mentor.*team",
        r"grow.*team",
        r"head of",
        r"manage.*org",
        r"org design",
    ],
    "build_new": [
        r"0.to.1",
        r"zero.to.one",
        r"greenfield",
        r"build.*from scratch",
        r"launch.*new",
        r"new product",
        r"incubat",
        r"create.*new",
        r"founding",
        r"early.stage",
        r"build.*new",
    ],
    "optimize_existing": [
        r"scale",
        r"optimiz",
        r"improv",
        r"efficien",
        r"mature",
        r"iterate",
        r"enhance",
        r"growth",
        r"retention",
    ],
    "enterprise": [
        r"enterprise",
        r"B2B",
        r"\bSaaS\b",
        r"Fortune\s*\d",
        r"large.scale",
        r"large customers",
    ],
    "consumer": [
        r"consumer",
        r"B2C",
        r"end.user",
        r"customer.facing app",
        r"mobile app",
        r"marketplace",
    ],
    "technical": [
        r"technic",
        r"engineer",
        r"architect",
        r"system design",
        r"data model",
        r"API",
        r"infrastructure",
        r"platform",
        r"ML\b",
        r"machine learning",
        r"algorithm",
        r"software",
        r"code review",
        r"technical spec",
        r"RFC",
    ],
}


# ---------------------------------------------------------------------------
# Greenhouse JSON helpers
# ---------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    """Remove HTML tags and decode entities. Handles double-escaped HTML."""
    # First decode entities (handles &lt; &gt; &amp; etc.) — may need two passes
    text = html.unescape(raw)
    text = html.unescape(text)  # double-escaped content like &amp;lt;
    # Now strip actual HTML tags
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</(?:p|div|h[1-6]|ul|ol|li|tr|td|th|table|section|header|footer|article)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # Clean up excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _try_parse_greenhouse_json(raw: str) -> str | None:
    """If *raw* looks like Greenhouse API JSON, extract the text content."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    # Single job object
    if isinstance(data, dict):
        jobs = [data]
    elif isinstance(data, list):
        jobs = data
    else:
        return None

    parts: list[str] = []
    for job in jobs:
        if "title" in job:
            parts.append(f"# {job['title']}")
        if "company_name" in job:
            parts.append(f"Company: {job['company_name']}")
        if "location" in job and isinstance(job["location"], dict):
            parts.append(f"Location: {job['location'].get('name', '')}")
        elif "location" in job and isinstance(job["location"], str):
            parts.append(f"Location: {job['location']}")

        content = job.get("content", "")
        if content:
            parts.append(_strip_html(content))

        # departments / offices
        for dept in job.get("departments", []):
            if isinstance(dept, dict) and dept.get("name"):
                parts.append(f"Department: {dept['name']}")

    if not parts:
        return None

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _normalise_text(raw: str) -> str:
    """Normalise the raw input: try Greenhouse JSON first, else treat as text."""
    greenhouse = _try_parse_greenhouse_json(raw)
    if greenhouse is not None:
        return greenhouse
    return raw


def _classify_sections(text: str) -> dict[str, list[str]]:
    """Split the text into classified sections and extract bullet items."""
    lines = text.split("\n")
    sections: dict[str, list[str]] = {
        "responsibilities": [],
        "required_qualifications": [],
        "preferred_qualifications": [],
        "_preamble": [],
    }

    current_section = "_preamble"

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this line is a section header
        matched_section = _match_section_header(stripped)
        if matched_section:
            current_section = matched_section
            continue

        # Extract bullet / list items
        item = _extract_bullet(stripped)
        if item and current_section in sections:
            sections[current_section].append(item)
        elif not item and current_section == "_preamble":
            sections["_preamble"].append(stripped)

    return sections


def _match_section_header(line: str) -> str | None:
    """Return the canonical section name if *line* looks like a section header."""
    # Strip markdown heading markers, colons, trailing whitespace
    clean = re.sub(r"^#+\s*", "", line)
    clean = re.sub(r"\s*:?\s*$", "", clean)
    lower = clean.translate(HEADER_NORMALIZATION_TRANSLATION).lower().strip()

    # Skip very long lines (probably not headers)
    if len(lower) > 80:
        return None

    for pattern, section in SECTION_PATTERNS:
        if re.search(pattern, lower):
            return section
    return None


def _extract_bullet(line: str) -> str | None:
    """If *line* looks like a bullet / list item, return the cleaned text."""
    # Common bullet prefixes: -, *, •, ▪, ►, numbered (1., 1))
    m = re.match(r"^\s*(?:[-*•▪►–—]\s+|\d+[.)]\s+)", line)
    if m:
        text = line[m.end() :].strip()
        return text if text else None
    return None


def _extract_title(text: str) -> str:
    """Extract job title from text."""
    lines = text.strip().split("\n")
    for line in lines[:10]:
        stripped = line.strip()
        # Markdown heading
        m = re.match(r"^#+\s+(.+)", stripped)
        if m:
            candidate = m.group(1).strip()
            # Skip if it looks like a section header
            if _match_section_header(candidate) is None and not title_looks_generic(candidate):
                return candidate
        # Short standalone line at the top that looks like a title
        if stripped and len(stripped) < 120 and not stripped.startswith("-"):
            lower = stripped.lower()
            title_words = [
                "manager",
                "engineer",
                "analyst",
                "scientist",
                "designer",
                "architect",
                "director",
                "lead",
                "head",
                "vp",
                "principal",
                "staff",
                "specialist",
                "coordinator",
                "strategist",
                "associate",
                "consultant",
                "developer",
                "product",
            ]
            if any(w in lower for w in title_words) and not title_looks_generic(stripped):
                return re.sub(r"^#+\s*", "", stripped).strip()
    return ""


def _extract_company(text: str) -> str:
    """Try to extract company name from the text."""
    # Pattern: "Company: X" or "**Company:** X" — require colon, same line, reasonable length
    m = re.search(r"(?:\*\*)?Company(?:\*\*)?:\s*(.{2,60})$", text, re.I | re.M)
    if m:
        candidate = _strip_generic_company_suffix(m.group(1).strip().strip("*").strip())
        # Reject if it looks like a sentence (too many words = not a company name)
        if len(candidate.split()) <= 5 and not company_name_looks_generic(candidate):
            return candidate

    # Wrapper headings often use "Careers at <Company>" above the real role title.
    for line in text.split("\n")[:10]:
        stripped = re.sub(r"^#+\s*", "", line.strip())
        m = re.match(r"^Careers at\s+([A-Z][A-Za-z0-9&.' -]{1,40})$", stripped, re.I)
        if m:
            candidate = m.group(1).strip()
            if not company_name_looks_generic(candidate):
                return candidate

    # Pattern: "About Rippling" / "With Rippling" near the top of the posting
    for line in text.split("\n")[:15]:
        stripped = line.strip()
        m = re.match(r"^(?:About|With)\s+([A-Z][A-Za-z0-9&.' -]{1,40})$", stripped)
        if m:
            candidate = m.group(1).strip()
            if not company_name_looks_generic(candidate):
                return candidate

    # Pattern: "at <Company>" in title line only
    for line in text.split("\n")[:10]:
        stripped = line.strip()
        m = re.match(
            r"^(?i:at)\s+"
            r"([A-Z0-9][A-Za-z0-9&.'()\-/,]*(?:\s+[A-Z0-9][A-Za-z0-9&.'()\-/,]*){0,5})"
            r"(?=[:.,]|\s+(?:we|you|our|the|this|they|it)\b|$)",
            stripped,
        )
        if m:
            candidate = m.group(1).strip()
            if not company_name_looks_generic(candidate):
                return candidate

    # Department line sometimes has company
    m = re.search(r"Department:\s*(.+)", text)
    if m:
        dept = m.group(1).strip()
        # If there's a dash, company might be before it
        if " - " in dept:
            candidate = dept.split(" - ")[0].strip()
            if not company_name_looks_generic(candidate):
                return candidate

    return ""


def _extract_location(text: str) -> str:
    """Try to extract location."""
    # Pattern: "Location: X"
    m = re.search(r"(?:\*\*)?Location(?:\*\*)?:?\s*(.+)", text, re.I)
    if m:
        val = m.group(1).strip().strip("*").strip()
        # Clean up trailing junk
        val = re.sub(r"\s*\|.*", "", val)
        return val

    # Look for common patterns
    for line in text.split("\n")[:20]:
        stripped = line.strip()
        # City, State pattern
        m = re.search(
            r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*(?:CA|NY|TX|WA|MA|IL|CO|GA|OR|VA|PA|NC|FL|OH|AZ|MN|UT|MD|NJ|CT|DC|WI|MO|TN|IN|MI|Remote))\b",
            stripped,
        )
        if m:
            return m.group(1)
        if re.search(r"\bremote\b", stripped, re.I) and len(stripped) < 60:
            return stripped

    return ""


def _extract_team(text: str) -> str:
    """Try to extract team / department / business unit."""
    m = re.search(r"Department:\s*(.+)", text)
    if m:
        return m.group(1).strip()

    m = re.search(r"(?:Team|Org|Business Unit|Group|Division):\s*(.+)", text, re.I)
    if m:
        return m.group(1).strip()

    # Look for "on the X team" or "within the X org"
    m = re.search(
        r"(?:on|join|within|part of)\s+(?:the\s+)?([A-Z][A-Za-z0-9 &/]+?)\s+(?:team|org|organization|group|department)",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    return ""


def _determine_level(title: str, text: str) -> str:
    """Determine seniority level from title (primarily) and text."""
    title_lower = title.lower()

    for level in LEVEL_PRIORITY:
        for kw in LEVEL_KEYWORDS[level]:
            if kw.lower() in title_lower:
                # "lead" by itself maps to senior
                if level == "lead":
                    return "senior"
                return level

    # Fallback: look for explicit level mentions in text
    text_lower = text.lower()
    for level in LEVEL_PRIORITY:
        for kw in LEVEL_KEYWORDS[level]:
            pattern = rf"\b{re.escape(kw.strip())}\b"
            if re.search(pattern, text_lower[:500]):
                if level == "lead":
                    return "senior"
                return level

    return "mid"  # default


def _extract_keywords(text: str) -> list[str]:
    """Extract technology / methodology / domain keywords from the text."""
    found: set[str] = set()

    for kw in TECH_KEYWORDS:
        # Use word boundary search for short keywords to avoid false positives
        if len(kw) <= 3:
            pattern = rf"\b{re.escape(kw)}\b"
        else:
            pattern = re.escape(kw)
        if re.search(pattern, text, re.I if len(kw) > 3 else 0):
            found.add(kw)

    # Also find capitalised multi-word terms that might be product names / acronyms
    # Only match within single lines to avoid cross-line artifacts
    for m in re.finditer(r"\b([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)+)\b", text):
        term = m.group(1)
        # Skip common English phrases
        skip = {
            "The",
            "This",
            "That",
            "What",
            "When",
            "Where",
            "How",
            "Who",
            "You",
            "Your",
            "Our",
            "We",
            "About",
            "Join",
            "Apply",
        }
        # Also skip common section header phrases
        skip_phrases = {
            "Minimum Qualifications",
            "Preferred Qualifications",
            "Required Qualifications",
            "Basic Qualifications",
            "Key Responsibilities",
            "About The Role",
            "Nice To Have",
            "What You",
            "Who You",
        }
        if term.split()[0] not in skip and len(term) < 40 and term not in skip_phrases:
            found.add(term)

    # Find quoted terms
    for m in re.finditer(r'"([^"]{2,40})"', text):
        found.add(m.group(1))

    # Find ALL-CAPS acronyms (3+ letters)
    for m in re.finditer(r"\b([A-Z]{3,10})\b", text):
        acr = m.group(1)
        noise = {
            "THE",
            "AND",
            "FOR",
            "YOU",
            "ARE",
            "NOT",
            "OUR",
            "HAS",
            "WAS",
            "CAN",
            "ALL",
            "HER",
            "HIS",
            "HIM",
            "WHO",
            "HOW",
            "NEW",
            "ONE",
            "TWO",
            "ANY",
            "USE",
            "MAY",
            "PER",
            "ITS",
            "GET",
            "SET",
            "PUT",
            "ADD",
            "RUN",
            "USD",
            "PST",
            "PDT",
            "EST",
            "CST",
            "MST",
            "CDT",
            "EDT",
            "MDT",
        }
        if acr not in noise:
            found.add(acr)

    return sorted(found)


def _compute_signals(text: str) -> dict[str, bool]:
    """Compute boolean signal flags from the full text."""
    text_lower = text.lower()
    signals: dict[str, bool] = {}

    for signal_name, patterns in SIGNAL_PATTERNS.items():
        signals[signal_name] = any(re.search(p, text_lower) for p in patterns)

    return signals


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_jd(raw: str) -> dict:
    """Parse raw job description text into structured data."""
    text = _normalise_text(raw)

    title = _extract_title(text)
    company = _extract_company(text)
    location = _extract_location(text)
    team = _extract_team(text)
    level = _determine_level(title, text)

    sections = _classify_sections(text)

    keywords = _extract_keywords(text)
    signals = _compute_signals(text)

    return {
        "title": title,
        "company": company,
        "location": location,
        "level": level,
        "team": team,
        "responsibilities": sections["responsibilities"],
        "required_qualifications": sections["required_qualifications"],
        "preferred_qualifications": sections["preferred_qualifications"],
        "keywords": keywords,
        "signals": signals,
    }


def _print_summary(data: dict) -> None:
    """Print a concise human-readable summary to stderr."""
    w = sys.stderr.write
    w("\n=== JD Parse Summary ===\n")
    w(f"  Title:    {data['title'] or '(not found)'}\n")
    w(f"  Company:  {data['company'] or '(not found)'}\n")
    w(f"  Location: {data['location'] or '(not found)'}\n")
    w(f"  Level:    {data['level']}\n")
    w(f"  Team:     {data['team'] or '(not found)'}\n")
    w(f"  Responsibilities:         {len(data['responsibilities'])} items\n")
    w(f"  Required qualifications:  {len(data['required_qualifications'])} items\n")
    w(f"  Preferred qualifications: {len(data['preferred_qualifications'])} items\n")
    w(f"  Keywords: {len(data['keywords'])} extracted\n")

    active = [k for k, v in data["signals"].items() if v]
    w(f"  Signals:  {', '.join(active) if active else '(none)'}\n")
    w("========================\n\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a job description into structured JSON.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input file path, or '-' to read from stdin (default: stdin)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output JSON file path. Parent dirs created automatically.",
    )
    args = parser.parse_args()

    # Read input
    if args.input == "-":
        raw = sys.stdin.read()
    else:
        path = Path(args.input)
        if not path.exists():
            print(f"Error: input file not found: {path}", file=sys.stderr)
            sys.exit(1)
        raw = path.read_text(encoding="utf-8")

    if not raw.strip():
        print("Error: empty input", file=sys.stderr)
        sys.exit(1)

    # Parse
    result = parse_jd(raw)

    # Output JSON
    json_str = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_str + "\n", encoding="utf-8")
        print(f"[parse_jd] Written to {out_path}", file=sys.stderr)
    else:
        print(json_str)

    # Always print summary to stderr
    _print_summary(result)


if __name__ == "__main__":
    main()
