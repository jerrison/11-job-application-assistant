from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_queue_sync_payload_writes_pending_file(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")
    pending_root = tmp_path / ".context" / "compound-engineering" / "linear-sync"
    payload = module.queue_sync_payload(
        pending_root,
        item_id="phase2:1",
        action="update_issue",
        body={"issue_id": "NAD-1", "state": "Todo"},
    )
    payload_path = Path(payload)
    assert payload_path == pending_root / "phase2__1--update_issue.json"
    assert payload_path.exists()
    assert json.loads(payload_path.read_text(encoding="utf-8")) == {
        "item_id": "phase2:1",
        "action": "update_issue",
        "body": {"issue_id": "NAD-1", "state": "Todo"},
    }
    assert payload_path.read_text(encoding="utf-8").endswith("\n")


def test_queue_sync_payload_sanitizes_action_path_segments(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")
    pending_root = tmp_path / ".context" / "compound-engineering" / "linear-sync"
    payload = module.queue_sync_payload(
        pending_root,
        item_id="phase2:1",
        action="sync/phase1..state",
        body={"issue_id": "NAD-1"},
    )

    payload_path = Path(payload)
    assert payload_path == pending_root / "phase2__1--sync_phase1__state.json"
    assert payload_path.exists()


def test_fetch_phase1_linear_todo_rows_uses_local_api_when_token_present():
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "identifier": "NAD-101",
                                "title": "Fix proof drift",
                                "state": {"name": "Todo"},
                                "labels": {"nodes": [{"name": "bug"}]},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }

    with patch.dict(os.environ, {"LINEAR_API_TOKEN": "token"}), patch("httpx.post", return_value=FakeResponse()) as post:
        rows = module.fetch_phase1_linear_todo_rows()

    assert rows[0]["linear_issue_id"] == "NAD-101"
    assert post.call_count == 1
    assert post.call_args.kwargs["headers"] == {"Authorization": "token", "Content-Type": "application/json"}
    assert post.call_args.kwargs["json"] == {"query": module.PHASE1_TODO_QUERY, "variables": {"after": None}}
    assert post.call_args.kwargs["timeout"] == 30.0


def test_fetch_phase1_linear_todo_rows_reads_bridge_export_when_token_missing(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")
    export_path = tmp_path / "phase1-todo-export.json"
    export_path.write_text(
        json.dumps(
            [
                {
                    "identifier": "NAD-202",
                    "title": "Fix queue drift",
                    "state": {"name": "Todo"},
                    "labels": {"nodes": [{"name": "requires-user-input"}]},
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.object(module, "PHASE1_BRIDGE_EXPORT", export_path), patch.dict(os.environ, {}, clear=True):
        rows = module.fetch_phase1_linear_todo_rows()

    assert rows[0]["linear_issue_id"] == "NAD-202"
    assert rows[0]["requires_user_input"] == "true"


def test_fetch_phase1_linear_todo_rows_raises_on_graphql_errors():
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errors": [{"message": "bad auth"}]}

    with patch.dict(os.environ, {"LINEAR_API_TOKEN": "token"}), patch("httpx.post", return_value=FakeResponse()):
        with pytest.raises(RuntimeError, match="bad auth"):
            module.fetch_phase1_linear_todo_rows()


def test_fetch_phase1_linear_todo_rows_raises_on_non_object_graphql_payload():
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return ["not", "an", "object"]

    with patch.dict(os.environ, {"LINEAR_API_TOKEN": "token"}), patch("httpx.post", return_value=FakeResponse()):
        with pytest.raises(RuntimeError, match="JSON object"):
            module.fetch_phase1_linear_todo_rows()


def test_fetch_phase1_linear_todo_rows_raises_when_next_page_cursor_is_missing():
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "issues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": True, "endCursor": None},
                    }
                }
            }

    with patch.dict(os.environ, {"LINEAR_API_TOKEN": "token"}), patch("httpx.post", return_value=FakeResponse()):
        with pytest.raises(RuntimeError, match="endCursor"):
            module.fetch_phase1_linear_todo_rows()
