#!/usr/bin/env python3
"""Rank master resume bullets by relevance to a parsed job description.

Uses TF-IDF cosine similarity — no LLM calls, no external packages.
Purely deterministic ranking using only the Python standard library.

Usage:
    uv run scripts/rank_bullets.py output/samsara/principal-pm-maintenance/content/jd_parsed.json -o output/samsara/principal-pm-maintenance/content/ranked_bullets.json
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import material_path, sync_state_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MASTER_RESUME_PATH = material_path("master_resume.md")
WORK_STORIES_PATH = material_path("work_stories.md")
BULLETS_CACHE_PATH = sync_state_path(".bullets_cache.json")
STORY_TERMS_CACHE_PATH = sync_state_path(".story_terms_cache.json")

# Position IDs mapped to heading patterns in master_resume.md
POSITION_HEADINGS = {
    "moodys": "MOODY",
    "kyte": "KYTE",
    "tmobile": "T-MOBILE",
    "lyft": "LYFT",
    "allstate": "ALLSTATE",
}

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "we",
        "you",
        "he",
        "she",
        "they",
        "me",
        "us",
        "him",
        "her",
        "them",
        "my",
        "our",
        "your",
        "his",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "not",
        "no",
        "nor",
        "if",
        "then",
        "than",
        "so",
        "as",
        "up",
        "out",
        "about",
        "into",
        "over",
        "after",
        "before",
        "between",
        "under",
        "above",
        "each",
        "all",
        "both",
        "such",
        "just",
        "also",
        "very",
        "too",
        "more",
        "most",
        "other",
        "some",
        "any",
        "only",
        "own",
        "same",
        "few",
        "s",
        "t",
        "d",
        "m",
        "re",
        "ve",
        "ll",
        "don",
        "won",
        "didn",
        "doesn",
        "wasn",
        "weren",
        "hasn",
        "haven",
        "shouldn",
        "couldn",
        "wouldn",
        "using",
        "including",
        "across",
        "through",
        "during",
        "while",
        "within",
        "without",
        "among",
        "along",
        "around",
        "against",
        "based",
        "new",
        "first",
        "well",
        "way",
        "use",
        "used",
        "work",
        "working",
        "team",
        "able",
        "e",
        "g",
    ]
)

# Story bonus weight — small multiplier for story-theme overlap
STORY_BONUS_WEIGHT = 0.10


# ---------------------------------------------------------------------------
# Caching helpers
# ---------------------------------------------------------------------------


def _load_cache(cache_path: Path, source_path: Path):
    """Return cached data if cache exists and source mtime matches, else None."""
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        if cache.get("source_mtime") == source_path.stat().st_mtime:
            return cache["data"]
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return None


def _save_cache(cache_path: Path, source_path: Path, data) -> None:
    """Write data to cache file alongside the source file's mtime."""
    cache = {
        "source_mtime": source_path.stat().st_mtime,
        "data": data,
    }
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, remove stopwords."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


# ---------------------------------------------------------------------------
# Resume parsing
# ---------------------------------------------------------------------------


def parse_master_resume(path: Path) -> dict[str, list[str]]:
    """Extract bullets per position from master_resume.md.

    Returns e.g. {"moodys": ["bullet1 text", "bullet2 text", ...], ...}
    """
    text = path.read_text()
    lines = text.split("\n")

    positions: dict[str, list[str]] = {}
    current_position: str | None = None

    for line in lines:
        # Detect position heading (## lines or plain-text company lines)
        heading_upper = line.upper().strip()
        is_md_heading = line.startswith("## ")
        is_plain_heading = (
            not is_md_heading
            and any(p in heading_upper for p in POSITION_HEADINGS.values())
            and not line.startswith("* ")
        )
        if is_md_heading or is_plain_heading:
            matched = False
            for pos_id, pattern in POSITION_HEADINGS.items():
                if pattern in heading_upper:
                    current_position = pos_id
                    positions.setdefault(pos_id, [])
                    matched = True
                    break
            if not matched:
                current_position = None
            continue

        # Stop at non-experience sections
        if line.startswith("# ") and current_position is not None:
            # Hit a new top-level section (EDUCATION, SKILLS, etc.)
            if "EXPERIENCE" not in line.upper():
                current_position = None
                continue

        # Extract bullets
        if current_position and line.startswith("* "):
            bullet_text = line[2:].strip()
            if bullet_text:
                positions[current_position].append(bullet_text)

    return positions


# ---------------------------------------------------------------------------
# Work stories parsing
# ---------------------------------------------------------------------------


def extract_story_terms(path: Path) -> list[str]:
    """Extract key terms from work_stories.md for story-boost scoring.

    Returns a flat list of tokens from all story content.
    """
    if not path.exists():
        return []
    text = path.read_text()
    return tokenize(text)


# ---------------------------------------------------------------------------
# TF-IDF engine
# ---------------------------------------------------------------------------


def compute_tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency: count / total tokens."""
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    total = len(tokens)
    return {t: c / total for t, c in counts.items()}


def compute_idf(documents: list[list[str]]) -> dict[str, float]:
    """Inverse document frequency across all documents.

    IDF(t) = log(N / df(t)) where df(t) = number of docs containing t.
    """
    n = len(documents)
    if n == 0:
        return {}

    df: dict[str, int] = {}
    for doc in documents:
        seen = set(doc)
        for t in seen:
            df[t] = df.get(t, 0) + 1

    return {t: math.log(n / df_count) for t, df_count in df.items()}


def tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """Compute TF-IDF vector for a token list."""
    tf = compute_tf(tokens)
    return {t: tf_val * idf.get(t, 0.0) for t, tf_val in tf.items()}


def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    # Dot product over shared keys
    shared_keys = set(vec_a.keys()) & set(vec_b.keys())
    if not shared_keys:
        return 0.0

    dot = sum(vec_a[k] * vec_b[k] for k in shared_keys)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0

    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# JD parsing
# ---------------------------------------------------------------------------


def build_jd_query(jd: dict) -> str:
    """Combine all JD fields into a single query document string."""
    parts: list[str] = []

    for field in [
        "responsibilities",
        "required_qualifications",
        "preferred_qualifications",
    ]:
        val = jd.get(field)
        if isinstance(val, list):
            parts.extend(val)
        elif isinstance(val, str):
            parts.append(val)

    keywords = jd.get("keywords")
    if isinstance(keywords, list):
        parts.extend(keywords)
    elif isinstance(keywords, str):
        parts.append(keywords)

    return " ".join(parts)


def extract_jd_keywords(jd: dict) -> set[str]:
    """Extract the explicit keywords list from the JD, lowercased."""
    keywords = jd.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    # Keep multi-word keywords as-is for matching, but also collect individual tokens
    result: set[str] = set()
    for kw in keywords:
        result.add(kw.lower().strip())
    return result


def find_matching_keywords(bullet: str, jd_keywords: set[str]) -> list[str]:
    """Find which JD keywords appear in a bullet (case-insensitive)."""
    bullet_lower = bullet.lower()
    matches = []
    for kw in sorted(jd_keywords):
        if kw in bullet_lower:
            matches.append(kw)
    return matches


# ---------------------------------------------------------------------------
# Main ranking logic
# ---------------------------------------------------------------------------


def rank_bullets(
    jd_path: str,
    jd: dict,
    positions: dict[str, list[str]],
    story_terms: list[str],
) -> dict:
    """Rank all bullets against the JD query and return output structure."""

    # Build the JD query document
    jd_query_text = build_jd_query(jd)
    jd_tokens = tokenize(jd_query_text)

    # Collect all bullet token lists + the query for IDF computation
    all_bullets: list[tuple[str, str]] = []  # (position_id, bullet_text)
    all_bullet_tokens: list[list[str]] = []

    for pos_id, bullets in positions.items():
        for bullet in bullets:
            all_bullets.append((pos_id, bullet))
            all_bullet_tokens.append(tokenize(bullet))

    # IDF computed over all bullets + the JD query
    all_documents = [jd_tokens] + all_bullet_tokens
    idf = compute_idf(all_documents)

    # Query TF-IDF vector
    query_vec = tfidf_vector(jd_tokens, idf)

    # Story term set for bonus scoring
    story_term_set = set(story_terms)

    # JD keywords for matching
    jd_keywords = extract_jd_keywords(jd)

    # Score each bullet
    scored: dict[str, list[dict]] = {}
    for idx, (pos_id, bullet) in enumerate(all_bullets):
        bullet_tokens = all_bullet_tokens[idx]
        bullet_vec = tfidf_vector(bullet_tokens, idf)

        # Base cosine similarity score
        score = cosine_similarity(query_vec, bullet_vec)

        # Story bonus: fraction of bullet tokens that overlap with story terms
        if bullet_tokens and story_term_set:
            overlap_count = sum(1 for t in bullet_tokens if t in story_term_set)
            overlap_ratio = overlap_count / len(bullet_tokens)
            score += STORY_BONUS_WEIGHT * overlap_ratio

        # Find matching JD keywords
        matching_kw = find_matching_keywords(bullet, jd_keywords)

        scored.setdefault(pos_id, []).append(
            {
                "score": round(score, 4),
                "bullet": bullet,
                "matching_keywords": matching_kw,
            }
        )

    # Sort by score descending within each position, assign ranks
    result_positions: dict[str, list[dict]] = {}
    for pos_id in positions:
        entries = scored.get(pos_id, [])
        entries.sort(key=lambda x: x["score"], reverse=True)
        for rank, entry in enumerate(entries, 1):
            entry["rank"] = rank
        # Reorder keys: rank first
        result_positions[pos_id] = [
            {
                "rank": e["rank"],
                "score": e["score"],
                "bullet": e["bullet"],
                "matching_keywords": e["matching_keywords"],
            }
            for e in entries
        ]

    return {
        "jd_source": jd_path,
        "positions": result_positions,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_summary(result: dict) -> None:
    """Print top 3 bullets per position to stdout."""
    print("\n" + "=" * 70)
    print("BULLET RANKING SUMMARY")
    print("=" * 70)

    for pos_id, entries in result["positions"].items():
        print(f"\n--- {pos_id.upper()} (top 3) ---")
        for entry in entries[:3]:
            truncated = entry["bullet"][:100] + "..." if len(entry["bullet"]) > 100 else entry["bullet"]
            kw_str = ", ".join(entry["matching_keywords"]) if entry["matching_keywords"] else "(none)"
            print(f"  #{entry['rank']}  score={entry['score']:.4f}  {truncated}")
            print(f"       keywords: {kw_str}")

    print("\n" + "=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank resume bullets by relevance to a parsed job description.")
    parser.add_argument(
        "jd_parsed_json",
        help="Path to parsed JD JSON (output of parse_jd.py)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output path for ranked bullets JSON",
    )
    args = parser.parse_args()

    # Read parsed JD
    jd_path = Path(args.jd_parsed_json)
    if not jd_path.exists():
        print(f"Error: JD file not found: {jd_path}", file=sys.stderr)
        sys.exit(1)

    with open(jd_path) as f:
        jd = json.load(f)

    # Read master resume
    if not MASTER_RESUME_PATH.exists():
        print(
            f"Error: master_resume.md not found at {MASTER_RESUME_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)

    cached_bullets = _load_cache(BULLETS_CACHE_PATH, MASTER_RESUME_PATH)
    if cached_bullets is not None:
        positions = cached_bullets
        print("[rank_bullets] Using cached bullets (master_resume.md unchanged)")
    else:
        positions = parse_master_resume(MASTER_RESUME_PATH)
        _save_cache(BULLETS_CACHE_PATH, MASTER_RESUME_PATH, positions)
        print("[rank_bullets] Parsed master_resume.md (cache updated)")
    if not positions:
        print("Error: no bullets extracted from master_resume.md", file=sys.stderr)
        sys.exit(1)

    # Read work stories for bonus scoring
    cached_story_terms = _load_cache(STORY_TERMS_CACHE_PATH, WORK_STORIES_PATH)
    if cached_story_terms is not None:
        story_terms = cached_story_terms
        print("[rank_bullets] Using cached story terms (work_stories.md unchanged)")
    else:
        story_terms = extract_story_terms(WORK_STORIES_PATH)
        if WORK_STORIES_PATH.exists():
            _save_cache(STORY_TERMS_CACHE_PATH, WORK_STORIES_PATH, story_terms)
            print("[rank_bullets] Parsed work_stories.md (cache updated)")
        else:
            print("[rank_bullets] work_stories.md not found, skipping story terms")

    # Rank
    result = rank_bullets(args.jd_parsed_json, jd, positions, story_terms)

    # Output
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(result, f, indent=4)
        print(f"Ranked bullets written to {out_path}")

    print_summary(result)


if __name__ == "__main__":
    main()
