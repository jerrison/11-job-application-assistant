#!/usr/bin/env python3
"""Linear sync helpers for the backlog sweep system."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_SYNC_DIR = PROJECT_ROOT / ".context" / "compound-engineering" / "linear-sync"
PHASE1_BRIDGE_EXPORT = PENDING_SYNC_DIR / "phase1-todo-export.json"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
PHASE1_TODO_QUERY = """
query BacklogSweepTodoIssues($after: String) {
  issues(
    first: 250
    after: $after
    filter: { state: { name: { eq: "Todo" } } }
  ) {
    nodes {
      identifier
      title
      state {
        name
      }
      labels {
        nodes {
          name
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sanitize_filename_component(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace("..", "__")


def queue_sync_payload(root: Path, *, item_id: str, action: str, body: dict[str, object]) -> str:
    safe_item_id = _sanitize_filename_component(item_id.replace(":", "__"))
    safe_action = _sanitize_filename_component(action)
    payload_path = root / f"{safe_item_id}--{safe_action}.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "item_id": item_id,
        "action": action,
        "body": body,
    }
    payload_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return str(payload_path)


def _normalize_issue_node(node: dict[str, object], *, captured_at_utc: str) -> dict[str, str]:
    labels = node.get("labels")
    label_nodes = labels.get("nodes", []) if isinstance(labels, dict) else []
    label_names = [str(label.get("name") or "").strip() for label in label_nodes if isinstance(label, dict)]
    normalized_labels = [label for label in label_names if label]
    state = node.get("state")
    state_name = str(state.get("name") or "").strip() if isinstance(state, dict) else ""
    return {
        "linear_issue_id": str(node.get("identifier") or "").strip(),
        "title": str(node.get("title") or "").strip(),
        "labels": ",".join(normalized_labels),
        "status": state_name,
        "related_job_id": "",
        "related_output_dir": "",
        "requires_user_input": "true" if "requires-user-input" in normalized_labels else "false",
        "captured_at_utc": captured_at_utc,
    }


def _graphql_error_message(errors: object) -> str:
    if not isinstance(errors, list):
        return "unknown GraphQL error"
    messages = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "").strip()
        if message:
            messages.append(message)
    return "; ".join(messages) if messages else "unknown GraphQL error"


def _fetch_issue_nodes_from_api(token: str) -> list[dict[str, object]]:
    cursor: str | None = None
    all_nodes: list[dict[str, object]] = []
    while True:
        payload = {"query": PHASE1_TODO_QUERY, "variables": {"after": cursor}}
        response = httpx.post(
            LINEAR_GRAPHQL_URL,
            # Personal API keys use a raw Authorization header; OAuth access tokens use Bearer.
            headers={"Authorization": token, "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Linear GraphQL response must be a JSON object")
        errors = data.get("errors")
        if errors:
            raise RuntimeError(f"Linear GraphQL returned errors: {_graphql_error_message(errors)}")
        issues = data.get("data", {}).get("issues")
        if not isinstance(issues, dict):
            raise RuntimeError("Linear GraphQL response missing data.issues")
        nodes = issues.get("nodes")
        page_info = issues.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise RuntimeError("Linear GraphQL response missing issues.nodes or issues.pageInfo")
        all_nodes.extend(nodes)
        if not page_info.get("hasNextPage"):
            break
        cursor = str(page_info.get("endCursor") or "").strip()
        if not cursor:
            raise RuntimeError("Linear GraphQL response set hasNextPage without a usable endCursor")
    return all_nodes


def fetch_phase1_linear_todo_rows() -> list[dict[str, str]]:
    token = os.environ.get("LINEAR_API_TOKEN", "").strip()
    captured_at = utc_now_iso()
    if token:
        nodes = _fetch_issue_nodes_from_api(token)
    else:
        if not PHASE1_BRIDGE_EXPORT.exists():
            raise RuntimeError(
                "LINEAR_API_TOKEN is not set and no bridge export exists at "
                f"{PHASE1_BRIDGE_EXPORT}. Sync Linear context before starting phase1."
            )
        export_text = PHASE1_BRIDGE_EXPORT.read_text(encoding="utf-8")
        parsed = json.loads(export_text)
        if not isinstance(parsed, list):
            raise ValueError(f"{PHASE1_BRIDGE_EXPORT} must contain a JSON list")
        nodes = parsed
    return [_normalize_issue_node(node, captured_at_utc=captured_at) for node in nodes]
