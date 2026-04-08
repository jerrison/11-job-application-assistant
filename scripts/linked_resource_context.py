#!/usr/bin/env python3
"""Shared linked-resource extraction and evidence helpers for submit-time answers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pdfplumber
from lxml import html as lxml_html

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import (
    LINKED_RESOURCE_CONTEXT_JSON,
    LINKED_RESOURCE_EVIDENCE_DIR,
    LINKED_RESOURCE_FAILURES_JSON,
    existing_submit_dirs,
    role_submit_dir,
)

LINKED_RESOURCE_URL_RE = re.compile(r"https?://[^\s<>\")]+", re.I)
MAX_LINKED_RESOURCES = 4
FETCH_TIMEOUT_SECONDS = 20
MAX_FETCH_BYTES = 512_000
HTML_TEXT_LIMIT = 3_000
TABLE_ROW_LIMIT = 25
PDF_PAGE_LIMIT = 4


@dataclass(frozen=True)
class LinkedResourceRequest:
    field_name: str
    label: str
    description: str
    required: bool
    question_text: str
    url: str

    @property
    def resource_key(self) -> str:
        parsed = urlparse(self.url)
        host = (parsed.netloc or "resource").casefold().replace(".", "_")
        path = re.sub(r"[^a-z0-9]+", "_", parsed.path.casefold()).strip("_") or "root"
        field_slug = re.sub(r"[^a-z0-9]+", "_", self.field_name.casefold()).strip("_") or "field"
        return f"{field_slug}__{host}_{path}".strip("_")[:120]


def _json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _request_signature(requests: list[LinkedResourceRequest]) -> str:
    normalized = [
        {
            "field_name": request.field_name,
            "url": request.url,
            "required": request.required,
            "label": request.label,
        }
        for request in requests
    ]
    return _sha256_bytes(_json_dumps_pretty(normalized).encode("utf-8"))


def extract_linked_resource_requests(question_specs: list[dict]) -> list[LinkedResourceRequest]:
    requests: list[LinkedResourceRequest] = []
    seen: set[tuple[str, str]] = set()
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        label = str(spec.get("label") or field_name).strip()
        description = str(spec.get("description") or "").strip()
        question_text = "\n".join(part for part in (label, description) if part)
        for match in LINKED_RESOURCE_URL_RE.findall(question_text):
            url = match.rstrip(").,")
            dedupe_key = (field_name, url)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            requests.append(
                LinkedResourceRequest(
                    field_name=field_name,
                    label=label,
                    description=description,
                    required=bool(spec.get("required")),
                    question_text=question_text,
                    url=url,
                )
            )
            if len(requests) >= MAX_LINKED_RESOURCES:
                return requests
    return requests


def _resource_cache_key(resource: dict) -> str:
    payload = {
        "url": resource.get("url"),
        "adapter": resource.get("adapter"),
        "content_fingerprint": resource.get("content_fingerprint"),
    }
    return _sha256_bytes(_json_dumps_pretty(payload).encode("utf-8"))


def _context_cache_key(resources: list[dict], failures: list[dict], request_signature: str) -> str:
    payload = {
        "request_signature": request_signature,
        "resource_keys": [
            {
                "url": resource.get("url"),
                "adapter": resource.get("adapter"),
                "content_fingerprint": resource.get("content_fingerprint"),
            }
            for resource in resources
        ],
        "failure_keys": [
            {
                "url": failure.get("url"),
                "adapter": failure.get("adapter"),
                "failure_reason": failure.get("failure_reason"),
            }
            for failure in failures
        ],
    }
    return _sha256_bytes(_json_dumps_pretty(payload).encode("utf-8"))


def _infer_adapter(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    content_type = content_type.casefold()
    if "db-fiddle.com" in host or "dbfiddle.uk" in host:
        return "db_fiddle"
    if path.endswith(".json") or "json" in content_type:
        return "generic_json"
    if path.endswith(".csv") or "csv" in content_type:
        return "generic_csv"
    if path.endswith(".pdf") or "pdf" in content_type:
        return "generic_pdf"
    return "generic_html"


def _normalize_csv_rows(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    header = [str(value).strip() for value in rows[0]]
    values = [[str(value).strip() for value in row] for row in rows[1 : TABLE_ROW_LIMIT + 1]]
    return header, values


def _extract_html_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text = raw_bytes.decode("utf-8", errors="replace")
    doc = lxml_html.fromstring(text)
    title = " ".join(doc.xpath("//title/text()")).strip()
    body_text = " ".join(segment.strip() for segment in doc.xpath("//body//text()") if segment.strip())
    body_text = re.sub(r"\s+", " ", body_text).strip()
    if len(body_text) > HTML_TEXT_LIMIT:
        body_text = body_text[:HTML_TEXT_LIMIT].rstrip() + "..."
    tables: list[dict] = []
    for table in doc.xpath("//table")[:5]:
        rows = []
        for row in table.xpath(".//tr")[:TABLE_ROW_LIMIT]:
            cells = [" ".join(cell.itertext()).strip() for cell in row.xpath("./th|./td")]
            if any(cell for cell in cells):
                rows.append(cells)
        if not rows:
            continue
        header = rows[0]
        body_rows = rows[1:]
        tables.append({"header": header, "rows": body_rows})
    payload = {
        "title": title or None,
        "body_text": body_text,
        "tables": tables,
    }
    prompt_parts = []
    if title:
        prompt_parts.append(f"Title: {title}")
    if body_text:
        prompt_parts.append(f"Body excerpt: {body_text}")
    if tables:
        rendered_tables = []
        for index, table in enumerate(tables, start=1):
            table_lines = [f"Table {index} header: {table['header']}"]
            for row in table["rows"][:5]:
                table_lines.append(f"Row: {row}")
            rendered_tables.append("\n".join(table_lines))
        prompt_parts.append("\n".join(rendered_tables))
    return payload, "\n".join(prompt_parts).strip()


def _extract_json_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text = raw_bytes.decode("utf-8", errors="replace")
    parsed = json.loads(text)
    if isinstance(parsed, list):
        sample = parsed[:5]
        facts = [
            {"label": "row_count", "value": len(parsed)},
        ]
    elif isinstance(parsed, dict):
        sample = parsed
        facts = [{"label": "top_level_keys", "value": list(parsed.keys())[:20]}]
    else:
        sample = parsed
        facts = []
    payload = {"data": parsed, "facts": facts}
    prompt = f"JSON payload summary:\n{_json_dumps_pretty(sample)[:3000]}"
    return payload, prompt


def _extract_csv_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text = raw_bytes.decode("utf-8", errors="replace")
    header, rows = _normalize_csv_rows(text)
    payload = {
        "header": header,
        "rows": rows,
        "row_count": max(0, sum(1 for _ in csv.reader(io.StringIO(text))) - 1),
    }
    prompt = f"CSV header: {header}\nSample rows:\n{_json_dumps_pretty(rows[:10])}"
    return payload, prompt


def _extract_pdf_payload(raw_bytes: bytes) -> tuple[dict, str]:
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages[:PDF_PAGE_LIMIT]:
            page_text = page.extract_text() or ""
            page_text = re.sub(r"\s+", " ", page_text).strip()
            if page_text:
                text_parts.append(page_text)
    text = "\n\n".join(text_parts).strip()
    if len(text) > HTML_TEXT_LIMIT:
        text = text[:HTML_TEXT_LIMIT].rstrip() + "..."
    payload = {"text": text, "page_count_sampled": min(PDF_PAGE_LIMIT, len(text_parts))}
    return payload, f"PDF excerpt:\n{text}"


def _request_bytes(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read(MAX_FETCH_BYTES)
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).strip()
    if not raw_bytes:
        raise ValueError("Fetched resource was empty.")
    return raw_bytes, content_type


def _default_fetcher(request: LinkedResourceRequest) -> dict:
    adapter = _infer_adapter(request.url)
    if adapter == "db_fiddle":
        from linked_resource_adapters.db_fiddle import fetch_db_fiddle_resource

        return fetch_db_fiddle_resource(request.url, question_text=request.question_text)
    from linked_resource_adapters.generic_fetch import fetch_generic_resource

    return fetch_generic_resource(request.url, question_text=request.question_text)


def _write_success_artifacts(evidence_dir: Path, request: LinkedResourceRequest, result: dict) -> dict:
    raw_bytes = result.get("raw_bytes") or b""
    raw_text = result.get("raw_text")
    raw_suffix = str(result.get("raw_suffix") or ".txt")
    raw_path = evidence_dir / f"{request.resource_key}{raw_suffix}"
    if raw_bytes:
        raw_path.write_bytes(raw_bytes)
        raw_digest = _sha256_bytes(raw_bytes)
    else:
        raw_path.write_text(str(raw_text or ""), encoding="utf-8")
        raw_digest = _sha256_bytes(str(raw_text or "").encode("utf-8"))
    payload_path = evidence_dir / f"{request.resource_key}.json"
    payload = {
        "url": request.url,
        "field_name": request.field_name,
        "label": request.label,
        "question_text": request.question_text,
        "adapter": result.get("adapter"),
        "content_type": result.get("content_type"),
        "normalized_payload": result.get("normalized_payload") or {},
        "derived_facts": result.get("derived_facts") or [],
        "deterministic_answer": str(result.get("deterministic_answer") or "").strip() or None,
        "prompt_context": result.get("prompt_context") or "",
        "written_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "raw_artifact": str(raw_path),
        "raw_sha256": raw_digest,
    }
    payload_path.write_text(_json_dumps_pretty(payload) + "\n", encoding="utf-8")
    resource = {
        "status": "fetched",
        "field_name": request.field_name,
        "label": request.label,
        "required": request.required,
        "url": request.url,
        "adapter": result.get("adapter"),
        "content_type": result.get("content_type"),
        "content_fingerprint": result.get("content_fingerprint") or raw_digest,
        "resource_key": request.resource_key,
        "prompt_context": result.get("prompt_context") or "",
        "derived_facts": result.get("derived_facts") or [],
        "deterministic_answer": str(result.get("deterministic_answer") or "").strip() or None,
        "payload_json": str(payload_path),
        "raw_artifact": str(raw_path),
    }
    resource["cache_key"] = _resource_cache_key(resource)
    return resource


def _build_failure_record(request: LinkedResourceRequest, *, adapter: str, reason: str) -> dict:
    return {
        "status": "failed",
        "field_name": request.field_name,
        "label": request.label,
        "required": request.required,
        "url": request.url,
        "adapter": adapter,
        "failure_reason": reason,
        "resource_key": request.resource_key,
    }


def _render_prompt_context(resources: list[dict], failures: list[dict]) -> str | None:
    sections: list[str] = []
    by_field: dict[str, list[dict]] = defaultdict(list)
    for resource in resources:
        by_field[str(resource.get("field_name") or "")].append(resource)
    for field_name, field_resources in by_field.items():
        del field_name
        for resource in field_resources:
            section = [
                f"Question: {resource.get('label')}",
                f"URL: {resource.get('url')}",
                f"Adapter: {resource.get('adapter')}",
            ]
            prompt_context = str(resource.get("prompt_context") or "").strip()
            if prompt_context:
                section.append(prompt_context)
            derived_facts = resource.get("derived_facts") or []
            if derived_facts:
                section.append(f"Derived facts: {_json_dumps_pretty(derived_facts)}")
            sections.append("\n".join(section))
    optional_failures = [failure for failure in failures if not failure.get("required")]
    if optional_failures:
        failure_lines = []
        for failure in optional_failures:
            failure_lines.append(
                f"Optional linked resource unavailable for {failure.get('label')}: "
                f"{failure.get('url')} ({failure.get('failure_reason')}). "
                "Return JSON null unless the answer is fully supported by other provided materials."
            )
        sections.append("\n".join(failure_lines))
    joined = "\n\n".join(section.strip() for section in sections if section.strip())
    return joined or None


def _validate_cached_payload(payload: dict, submit_dir: Path) -> bool:
    if not isinstance(payload, dict):
        return False
    resources = payload.get("resources")
    if not isinstance(resources, list):
        return False
    for resource in resources:
        if not isinstance(resource, dict):
            return False
        for key in ("payload_json", "raw_artifact"):
            raw_path = str(resource.get(key) or "").strip()
            if raw_path and not Path(raw_path).exists():
                return False
            if raw_path and not str(Path(raw_path)).startswith(str(submit_dir)):
                return False
    failures = payload.get("failures")
    return isinstance(failures, list)


def _load_cached_context(path: Path, *, request_signature: str) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(payload.get("request_signature") or "") != request_signature:
        return None
    if not _validate_cached_payload(payload, path.parent):
        return None
    return payload


def _copy_cached_artifacts(candidate_payload: dict, current_submit_dir: Path) -> dict:
    evidence_dir = current_submit_dir / LINKED_RESOURCE_EVIDENCE_DIR
    evidence_dir.mkdir(parents=True, exist_ok=True)
    resources: list[dict] = []
    for resource in candidate_payload.get("resources") or []:
        updated = dict(resource)
        for key in ("payload_json", "raw_artifact"):
            raw_path = str(resource.get(key) or "").strip()
            if not raw_path:
                continue
            source = Path(raw_path)
            destination = evidence_dir / source.name
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            updated[key] = str(destination)
        resources.append(updated)
    copied = dict(candidate_payload)
    copied["resources"] = resources
    copied["artifacts"] = {
        "context_json": str(current_submit_dir / LINKED_RESOURCE_CONTEXT_JSON),
        "failures_json": str(current_submit_dir / LINKED_RESOURCE_FAILURES_JSON),
        "evidence_dir": str(evidence_dir),
    }
    return copied


def clear_linked_resource_artifacts(out_dir: str | Path) -> None:
    submit_dir = role_submit_dir(out_dir)
    for filename in (LINKED_RESOURCE_CONTEXT_JSON, LINKED_RESOURCE_FAILURES_JSON):
        try:
            (submit_dir / filename).unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(submit_dir / LINKED_RESOURCE_EVIDENCE_DIR, ignore_errors=True)


def prepare_linked_resource_context(
    out_dir: str | Path,
    question_specs: list[dict],
    *,
    force_refresh: bool = False,
    fetcher=None,
) -> dict:
    out_dir = Path(out_dir)
    submit_dir = role_submit_dir(out_dir)
    submit_dir.mkdir(parents=True, exist_ok=True)
    requests = extract_linked_resource_requests(question_specs)
    if not requests:
        return {
            "request_signature": None,
            "cache_key": None,
            "prompt_context": None,
            "resources": [],
            "failures": [],
            "artifacts": {},
            "blockers": [],
            "used_cached_artifacts": False,
        }

    fetcher = fetcher or _default_fetcher
    request_signature = _request_signature(requests)
    context_path = submit_dir / LINKED_RESOURCE_CONTEXT_JSON
    failures_path = submit_dir / LINKED_RESOURCE_FAILURES_JSON
    evidence_dir = submit_dir / LINKED_RESOURCE_EVIDENCE_DIR

    if not force_refresh:
        current_payload = _load_cached_context(context_path, request_signature=request_signature)
        if current_payload is not None:
            current_payload["used_cached_artifacts"] = True
            return current_payload
        for candidate_submit_dir in existing_submit_dirs(out_dir):
            if candidate_submit_dir == submit_dir:
                continue
            candidate_context_path = candidate_submit_dir / LINKED_RESOURCE_CONTEXT_JSON
            candidate_payload = _load_cached_context(candidate_context_path, request_signature=request_signature)
            if candidate_payload is None:
                continue
            copied = _copy_cached_artifacts(candidate_payload, submit_dir)
            context_path.write_text(_json_dumps_pretty(copied) + "\n", encoding="utf-8")
            failures_path.write_text(_json_dumps_pretty(copied.get("failures") or []) + "\n", encoding="utf-8")
            copied["used_cached_artifacts"] = True
            return copied

    clear_linked_resource_artifacts(out_dir)
    submit_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    resources: list[dict] = []
    failures: list[dict] = []
    blockers: list[dict] = []
    for request in requests:
        adapter_hint = _infer_adapter(request.url)
        try:
            result = fetcher(request)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
            result = {
                "status": "failed",
                "adapter": adapter_hint,
                "failure_reason": str(exc).strip() or exc.__class__.__name__,
            }
        if str(result.get("status") or "fetched") == "failed":
            failure = _build_failure_record(
                request,
                adapter=str(result.get("adapter") or adapter_hint),
                reason=str(result.get("failure_reason") or "Linked resource fetch failed.").strip(),
            )
            failures.append(failure)
            if request.required:
                blockers.append(failure)
            continue
        resources.append(_write_success_artifacts(evidence_dir, request, result))

    prompt_context = _render_prompt_context(resources, failures)
    cache_key = _context_cache_key(resources, failures, request_signature)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "request_signature": request_signature,
        "cache_key": cache_key,
        "prompt_context": prompt_context,
        "resources": resources,
        "failures": failures,
        "artifacts": {
            "context_json": str(context_path),
            "failures_json": str(failures_path),
            "evidence_dir": str(evidence_dir),
        },
        "blockers": blockers,
        "used_cached_artifacts": False,
    }
    context_path.write_text(_json_dumps_pretty(payload) + "\n", encoding="utf-8")
    failures_path.write_text(_json_dumps_pretty(failures) + "\n", encoding="utf-8")
    return payload
