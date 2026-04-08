#!/usr/bin/env python3
"""db-fiddle adapter for linked-resource submit-time answers."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _capture_db_fiddle_payload(url: str) -> tuple[str, dict]:
    result: dict[str, str] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def handle_response(response) -> None:
            response_url = response.url
            if "prod-api.db-fiddle.com" not in response_url:
                return
            try:
                result["api_response"] = response.text()
            except Exception:
                return

        page.on("response", handle_response)
        page.goto(url, wait_until="networkidle", timeout=45_000)
        page.wait_for_timeout(2_000)
        html = page.content()
        browser.close()
    api_payload = json.loads(result.get("api_response") or "{}")
    return html, api_payload


def _extract_content(api_payload: dict) -> dict:
    data = api_payload.get("data") or []
    attributes = ((data[0] or {}).get("attributes") or {}) if data else {}
    content = attributes.get("content") or {}
    return {
        "schema_sql": str(content.get("schema") or "").strip(),
        "query_sql": str(content.get("query") or "").strip(),
        "title": str(attributes.get("title") or "").strip() or None,
        "engine": str(attributes.get("engine") or "").strip() or None,
    }


def _derive_known_facts(schema_sql: str, question_text: str) -> list[dict]:
    facts: list[dict] = []
    if not schema_sql.strip():
        return facts
    normalized = question_text.casefold()
    try:
        conn = sqlite3.connect(":memory:")
        conn.executescript(schema_sql)
        cur = conn.cursor()
        if "which card has the most spend" in normalized:
            row = cur.execute(
                """
                SELECT card_id, SUM(amount) AS total_spend
                FROM transactions
                GROUP BY card_id
                ORDER BY total_spend DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                facts.append(
                    {
                        "question": "Which card has the most spend?",
                        "answer": str(row[0]),
                        "detail": f"Total spend {row[1]}",
                    }
                )
        if "most number of individual transactions" in normalized:
            row = cur.execute(
                """
                SELECT cp.display_name, COUNT(t.id) AS tx_count
                FROM transactions t
                JOIN cards c ON t.card_id = c.id
                JOIN card_programs cp ON c.card_program_id = cp.id
                GROUP BY cp.id, cp.display_name
                ORDER BY tx_count DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                facts.append(
                    {
                        "question": "Which card program has the most number of individual transactions?",
                        "answer": str(row[0]),
                        "detail": f"Transaction count {row[1]}",
                    }
                )
        if "most transactions in october" in normalized:
            row = cur.execute(
                """
                SELECT cp.display_name, COUNT(t.id) AS tx_count
                FROM transactions t
                JOIN cards c ON t.card_id = c.id
                JOIN card_programs cp ON c.card_program_id = cp.id
                WHERE strftime('%m', t.user_transaction_time) = '10'
                GROUP BY cp.id, cp.display_name
                ORDER BY tx_count DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                facts.append(
                    {
                        "question": "Which card program had the most transactions in October?",
                        "answer": str(row[0]),
                        "detail": f"October transaction count {row[1]}",
                    }
                )
    finally:
        conn.close()
    return facts


def _format_tied_program_answer(names: list[str], count: int, *, period: bool = True) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]} with {count} transactions{'.' if period else ''}"
    if len(names) == 2:
        return f"{names[0]} (and {names[1]} tied) with {count} transactions each{'.' if period else ''}"
    head = ", ".join(names[:-1])
    return f"{head}, and {names[-1]} tied with {count} transactions each{'.' if period else ''}"


def _derive_ramp_screening_answer(schema_sql: str, question_text: str) -> str | None:
    if not schema_sql.strip():
        return None
    normalized = question_text.casefold()
    required_fragments = (
        "which card has the most spend",
        "most number of individual transactions",
        "most transactions in october",
    )
    if not all(fragment in normalized for fragment in required_fragments):
        return None

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_sql)
        cur = conn.cursor()

        top_card = cur.execute(
            """
            SELECT card_id, SUM(amount) AS total_spend
            FROM transactions
            GROUP BY card_id
            ORDER BY total_spend DESC, card_id ASC
            LIMIT 1
            """
        ).fetchone()
        if top_card is None:
            return None
        linked_program = cur.execute(
            """
            SELECT card_program_id
            FROM cards
            WHERE id = ?
            LIMIT 1
            """,
            (top_card[0],),
        ).fetchone()
        card_answer = f"Card {top_card[0]} with ${top_card[1]:,.2f} total spend."
        if linked_program is None:
            card_answer += (
                " (Note: This card is not linked to any program in the cards table, "
                "so it doesn't count toward any program total.)"
            )

        program_rows = cur.execute(
            """
            SELECT cp.display_name, COUNT(t.id) AS tx_count, COALESCE(SUM(t.amount), 0) AS total_spend
            FROM transactions t
            JOIN cards c ON t.card_id = c.id
            JOIN card_programs cp ON c.card_program_id = cp.id
            GROUP BY cp.id, cp.display_name
            ORDER BY tx_count DESC, total_spend DESC, cp.display_name ASC
            """
        ).fetchall()
        if not program_rows:
            return None
        max_tx_count = int(program_rows[0][1])
        top_program_names = [str(row[0]) for row in program_rows if int(row[1]) == max_tx_count]
        top_program_answer = _format_tied_program_answer(top_program_names, max_tx_count, period=True)

        october_rows = cur.execute(
            """
            SELECT cp.display_name, COUNT(t.id) AS tx_count, COALESCE(SUM(t.amount), 0) AS total_spend
            FROM transactions t
            JOIN cards c ON t.card_id = c.id
            JOIN card_programs cp ON c.card_program_id = cp.id
            WHERE strftime('%m', t.user_transaction_time) = '10'
            GROUP BY cp.id, cp.display_name
            ORDER BY tx_count DESC, total_spend DESC, cp.display_name ASC
            """
        ).fetchall()
        if not october_rows:
            return None
        max_october_count = int(october_rows[0][1])
        october_program_names = [str(row[0]) for row in october_rows if int(row[1]) == max_october_count]
        if len(october_program_names) == 1:
            october_answer = f"{october_program_names[0]} with {max_october_count} transactions in October 2021."
        else:
            october_answer = (
                f"{_format_tied_program_answer(october_program_names, max_october_count, period=False)} "
                "in October 2021."
            )

        return "\n".join(
            (
                "Which card has the most spend?",
                card_answer,
                "Which card program has the most number of individual transactions?",
                top_program_answer,
                "Which card program had the most transactions in October?",
                october_answer,
            )
        )
    finally:
        conn.close()


def fetch_db_fiddle_resource(url: str, *, question_text: str) -> dict:
    html, api_payload = _capture_db_fiddle_payload(url)
    content = _extract_content(api_payload)
    derived_facts = _derive_known_facts(content.get("schema_sql") or "", question_text)
    deterministic_answer = _derive_ramp_screening_answer(content.get("schema_sql") or "", question_text)
    prompt_lines = [
        f"db-fiddle engine: {content.get('engine') or 'unknown'}",
    ]
    if content.get("title"):
        prompt_lines.append(f"Title: {content['title']}")
    if content.get("schema_sql"):
        prompt_lines.append(f"Schema SQL:\n{content['schema_sql'][:4000]}")
    if content.get("query_sql"):
        prompt_lines.append(f"Query SQL:\n{content['query_sql'][:2000]}")
    if derived_facts:
        prompt_lines.append(f"Derived facts:\n{json.dumps(derived_facts, ensure_ascii=False, indent=2)}")
    if deterministic_answer:
        prompt_lines.append(f"Deterministic answer:\n{deterministic_answer}")
    return {
        "status": "fetched",
        "adapter": "db_fiddle",
        "content_type": "text/html",
        "raw_text": html,
        "raw_suffix": ".html",
        "normalized_payload": {
            "api_payload": api_payload,
            "schema_sql": content.get("schema_sql"),
            "query_sql": content.get("query_sql"),
            "engine": content.get("engine"),
            "title": content.get("title"),
        },
        "derived_facts": derived_facts,
        "deterministic_answer": deterministic_answer,
        "prompt_context": "\n\n".join(line for line in prompt_lines if line.strip()),
        "content_fingerprint": re.sub(r"[^a-f0-9]", "", json.dumps(api_payload, sort_keys=True).encode().hex())[:64],
    }
