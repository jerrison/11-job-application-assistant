#!/usr/bin/env python3
"""Generic direct-link fetch adapters for structured public resources."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pdfplumber
from lxml import html as lxml_html

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

MAX_FETCH_BYTES = 512_000
HTML_TEXT_LIMIT = 3_000
TABLE_ROW_LIMIT = 25
PDF_PAGE_LIMIT = 4
AGGREGATE_QUESTION_RE = re.compile(
    r"which\s+(?P<entity>.+?)\s+has\s+the\s+(?:most|highest|largest)\s+(?P<metric>.+?)(?:\?|$)",
    re.I,
)


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _coerce_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _format_numeric(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _match_column(
    columns: list[str], phrase: str, *, numeric_columns: set[str] | None = None, exclude: set[str] | None = None
) -> str | None:
    exclude = exclude or set()
    phrase_tokens = set(_tokenize(phrase))
    best_column = None
    best_score = 0
    for column in columns:
        if column in exclude:
            continue
        if numeric_columns is not None and column not in numeric_columns:
            continue
        column_tokens = set(_tokenize(column.replace("_", " ")))
        score = len(phrase_tokens & column_tokens)
        if score > best_score:
            best_score = score
            best_column = column
    return best_column if best_score > 0 else None


def _derive_table_aggregate_answer(question_text: str | None, rows: list[dict]) -> tuple[str | None, dict | None]:
    if not question_text or not rows:
        return None, None
    match = AGGREGATE_QUESTION_RE.search(question_text)
    if not match:
        return None, None

    ordered_columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in ordered_columns:
                ordered_columns.append(key)
    if not ordered_columns:
        return None, None

    numeric_columns = {
        column for column in ordered_columns if any(_coerce_float(row.get(column)) is not None for row in rows)
    }
    if not numeric_columns:
        return None, None

    metric_column = _match_column(ordered_columns, match.group("metric"), numeric_columns=numeric_columns)
    if metric_column is None and len(numeric_columns) == 1:
        metric_column = next(iter(numeric_columns))
    if not metric_column:
        return None, None

    entity_candidates = [column for column in ordered_columns if column != metric_column]
    entity_column = _match_column(entity_candidates, match.group("entity"))
    if entity_column is None and len(entity_candidates) == 1:
        entity_column = entity_candidates[0]
    if not entity_column:
        return None, None

    scored_rows: list[tuple[float, str]] = []
    for row in rows:
        metric_value = _coerce_float(row.get(metric_column))
        entity_value = str(row.get(entity_column) or "").strip()
        if metric_value is None or not entity_value:
            continue
        scored_rows.append((metric_value, entity_value))
    if not scored_rows:
        return None, None

    max_value = max(metric_value for metric_value, _entity_value in scored_rows)
    winners = [entity_value for metric_value, entity_value in scored_rows if abs(metric_value - max_value) < 1e-9]
    winner_text = winners[0] if len(winners) == 1 else f"{winners[0]} (and {', '.join(winners[1:])} tied)"
    metric_label = metric_column.replace("_", " ")
    value_text = _format_numeric(max_value)
    answer = f"{winner_text} with {metric_label} {value_text}."
    fact = {
        "question": match.group(0).strip(),
        "answer": winners[0],
        "detail": f"{metric_label} {value_text}",
    }
    return answer, fact


def _json_rows_for_deterministic_analysis(parsed) -> list[dict]:
    if isinstance(parsed, list) and all(isinstance(row, dict) for row in parsed):
        return [dict(row) for row in parsed]
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list) and all(isinstance(row, dict) for row in value):
                return [dict(row) for row in value]
    return []


def _csv_rows_for_deterministic_analysis(header: list[str], rows: list[list[str]]) -> list[dict]:
    if not header:
        return []
    structured_rows = []
    for row in rows:
        if not row:
            continue
        padded = list(row) + [""] * max(0, len(header) - len(row))
        structured_rows.append(dict(zip(header, padded[: len(header)], strict=False)))
    return structured_rows


def _request_bytes(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return response.read(MAX_FETCH_BYTES), str(getattr(response, "headers", {}).get("Content-Type", ""))


def _extract_html_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text = raw_bytes.decode("utf-8", errors="replace")
    doc = lxml_html.fromstring(text)
    title = " ".join(doc.xpath("//title/text()")).strip()
    body_text = " ".join(segment.strip() for segment in doc.xpath("//body//text()") if segment.strip())
    body_text = re.sub(r"\s+", " ", body_text).strip()
    if len(body_text) > HTML_TEXT_LIMIT:
        body_text = body_text[:HTML_TEXT_LIMIT].rstrip() + "..."
    tables = []
    for table in doc.xpath("//table")[:5]:
        rows = []
        for row in table.xpath(".//tr")[:TABLE_ROW_LIMIT]:
            cells = [" ".join(cell.itertext()).strip() for cell in row.xpath("./th|./td")]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append({"header": rows[0], "rows": rows[1:]})
    payload = {"title": title or None, "body_text": body_text, "tables": tables}
    prompt_parts = []
    if title:
        prompt_parts.append(f"Title: {title}")
    if body_text:
        prompt_parts.append(f"Body excerpt: {body_text}")
    if tables:
        prompt_parts.append(f"Tables: {json.dumps(tables[:3], ensure_ascii=False)}")
    return payload, "\n".join(prompt_parts)


def _extract_json_payload(
    raw_bytes: bytes, *, question_text: str | None = None
) -> tuple[dict, str, list[dict], str | None]:
    parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    if isinstance(parsed, list):
        summary = {"row_count": len(parsed), "sample_rows": parsed[:5]}
    elif isinstance(parsed, dict):
        summary = {"top_level_keys": list(parsed.keys())[:20], "sample": parsed}
    else:
        summary = {"value": parsed}
    rows = _json_rows_for_deterministic_analysis(parsed)
    deterministic_answer, deterministic_fact = _derive_table_aggregate_answer(question_text, rows)
    derived_facts = [summary]
    if deterministic_fact:
        derived_facts.append(deterministic_fact)
    prompt_context = f"JSON summary:\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
    if deterministic_answer:
        prompt_context += f"\nDeterministic answer:\n{deterministic_answer}"
    return {"data": parsed, "summary": summary}, prompt_context, derived_facts, deterministic_answer


def _extract_csv_payload(
    raw_bytes: bytes, *, question_text: str | None = None
) -> tuple[dict, str, list[dict], str | None]:
    text = raw_bytes.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    data_rows = rows[1 : TABLE_ROW_LIMIT + 1] if rows else []
    payload = {"header": header, "rows": data_rows, "row_count": max(0, len(rows) - 1)}
    structured_rows = _csv_rows_for_deterministic_analysis(header, data_rows)
    deterministic_answer, deterministic_fact = _derive_table_aggregate_answer(question_text, structured_rows)
    derived_facts = [{"label": "row_count", "value": payload["row_count"]}]
    if deterministic_fact:
        derived_facts.append(deterministic_fact)
    prompt = f"CSV header: {header}\nSample rows: {json.dumps(data_rows[:10], ensure_ascii=False)}"
    if deterministic_answer:
        prompt += f"\nDeterministic answer:\n{deterministic_answer}"
    return payload, prompt, derived_facts, deterministic_answer


def _extract_pdf_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text_parts = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages[:PDF_PAGE_LIMIT]:
            page_text = page.extract_text() or ""
            page_text = re.sub(r"\s+", " ", page_text).strip()
            if page_text:
                text_parts.append(page_text)
    text = "\n\n".join(text_parts).strip()
    if len(text) > HTML_TEXT_LIMIT:
        text = text[:HTML_TEXT_LIMIT].rstrip() + "..."
    return {"text": text}, f"PDF excerpt:\n{text}"


def fetch_generic_resource(url: str, *, question_text: str | None = None) -> dict:
    raw_bytes, content_type = _request_bytes(url)
    lowered = content_type.casefold()
    path = url.casefold()
    if path.endswith(".json") or "json" in lowered:
        payload, prompt_context, derived_facts, deterministic_answer = _extract_json_payload(
            raw_bytes,
            question_text=question_text,
        )
        return {
            "status": "fetched",
            "adapter": "generic_json",
            "content_type": content_type,
            "raw_bytes": raw_bytes,
            "raw_suffix": ".json",
            "normalized_payload": payload,
            "derived_facts": derived_facts,
            "deterministic_answer": deterministic_answer,
            "prompt_context": prompt_context,
        }
    if path.endswith(".csv") or "csv" in lowered:
        payload, prompt_context, derived_facts, deterministic_answer = _extract_csv_payload(
            raw_bytes,
            question_text=question_text,
        )
        return {
            "status": "fetched",
            "adapter": "generic_csv",
            "content_type": content_type,
            "raw_bytes": raw_bytes,
            "raw_suffix": ".csv",
            "normalized_payload": payload,
            "derived_facts": derived_facts,
            "deterministic_answer": deterministic_answer,
            "prompt_context": prompt_context,
        }
    if path.endswith(".pdf") or "pdf" in lowered:
        payload, prompt_context = _extract_pdf_payload(raw_bytes)
        return {
            "status": "fetched",
            "adapter": "generic_pdf",
            "content_type": content_type,
            "raw_bytes": raw_bytes,
            "raw_suffix": ".pdf",
            "normalized_payload": payload,
            "derived_facts": [],
            "prompt_context": prompt_context,
        }
    payload, prompt_context = _extract_html_payload(raw_bytes)
    return {
        "status": "fetched",
        "adapter": "generic_html",
        "content_type": content_type or "text/html",
        "raw_bytes": raw_bytes,
        "raw_suffix": ".html",
        "normalized_payload": payload,
        "derived_facts": [{"label": "table_count", "value": len(payload.get("tables") or [])}],
        "prompt_context": prompt_context,
    }
