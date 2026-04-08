"""Interactive Textual TUI for the job application system."""

from __future__ import annotations

import logging
import os
import platform
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Database helpers — thin wrappers so the TUI never imports job_db at module
# level (avoids circular-import headaches and keeps the DB path configurable).
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import saved_portal_import  # noqa: E402
from app_paths import jobs_db_path, worker_pid_path  # noqa: E402
from application_submit_common import (  # noqa: E402
    load_pending_user_input_for_submit_attempt,
    resolve_current_submit_artifacts,
)
from job_action_audit import build_action_detail_json, build_action_process_info
from job_db import (  # noqa: E402
    JOB_STATUSES,
    RETRY_AFTER_SENTINEL,
    add_job,
    ensure_job_metrics,
    get_all_job_metrics,
    get_board_counts,
    get_board_error_rates,
    get_job,
    get_job_metrics,
    get_job_timeline,
    get_jobs_processed_counts,
    get_phase_avg_durations,
    get_status_counts,
    get_summary_stats,
    init_db,
    log_event,
    open_db,
    query_jobs,
    update_job_metrics,
    update_status,
)
from llm_provider import VALID_PROVIDERS  # noqa: E402
from output_layout import role_submit_dir  # noqa: E402
from pipeline_draft_proof import draft_review_state  # noqa: E402
from pipeline_orchestrator import (  # noqa: E402
    approve_job,
    approve_job_failure_message,
    regenerate_job,
    reset_job_to_new,
)
from runtime_entrypoints import python_script_command  # noqa: E402
from runtime_policy import ensure_action_allowed  # noqa: E402
from runtime_trace import configure_runtime_trace, emit_trace  # noqa: E402
from settings_store import import_material, load_bootstrap, load_settings, save_settings  # noqa: E402

SAVED_PORTAL_SPECS = tuple(saved_portal_import.list_saved_portals())
DB_PATH = jobs_db_path()
_SETTINGS_MATERIAL_KEYS = (
    ("Master resume", "master_resume"),
    ("Work stories", "work_stories"),
    ("Candidate context", "candidate_context"),
    ("Application profile", "application_profile"),
)
_SETTINGS_MATERIAL_LABELS = dict(_SETTINGS_MATERIAL_KEYS)
_SETTINGS_CREDENTIAL_FIELDS = (
    ("openai_api_key", "settings-openai-api-key"),
    ("openai_api_keys", "settings-openai-api-keys"),
    ("gemini_api_key", "settings-gemini-api-key"),
    ("codex_api_key", "settings-codex-api-key"),
    ("anthropic_api_key", "settings-anthropic-api-key"),
    ("steel_api_key", "settings-steel-api-key"),
)


def _get_conn() -> sqlite3.Connection:
    """Return a lightweight connection (PRAGMAs only, no migrations)."""
    return open_db(DB_PATH)


def _worker_pid_file() -> Path:
    return worker_pid_path()


def _get_recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Fetch recent events across all jobs, joined with job info."""
    rows = conn.execute(
        """SELECT e.*, j.company, j.role_title
           FROM events e
           LEFT JOIN jobs j ON e.job_id = j.id
           ORDER BY e.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _tui_action_audit(surface: str, action_name: str, *, trigger: str = "button") -> tuple[dict | None, str | None]:
    detail_json = build_action_detail_json(surface=surface, trigger=trigger, route=f"tui://{surface}/{action_name}")
    return detail_json, build_action_process_info(detail_json)


# ---------------------------------------------------------------------------
# Status display helpers
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    "queued": "white",
    "resolving": "cyan",
    "generating": "cyan",
    "autofilling": "cyan",
    "draft": "dark_orange",
    "approved": "yellow",
    "submitting": "yellow",
    "submitted": "green",
    "retrying": "yellow",
    "fix_in_progress": "yellow",
    "stopped": "red",
    "needs_board_url": "magenta",
    "awaiting_captcha": "dark_orange3",
}

_STATUS_EMOJI = {
    "queued": "[ ]",
    "resolving": "[~]",
    "generating": "[~]",
    "autofilling": "[~]",
    "draft": "[?]",
    "approved": "[>]",
    "submitting": "[~]",
    "submitted": "[+]",
    "retrying": "[!]",
    "fix_in_progress": "[!]",
    "stopped": "[x]",
    "needs_board_url": "[?]",
    "awaiting_captcha": "[!]",
}

_PROCESSING_STATUSES = {
    "resolving",
    "generating",
    "submitting",
    "autofilling",
    "fix_in_progress",
    "retrying",
    "approved",
}
_STOPPED_STATUSES = {"stopped"}
_ATTENTION_STATUSES = {"needs_board_url", "awaiting_captcha"}


def _format_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        normalized = ts.replace("T", " ").replace(" UTC", "").rstrip("Z")
        date_part, time_part = normalized.split(" ", 1)
        return f"{date_part} {time_part[:8]}Z"
    except Exception:
        return ts[:20] if ts else ""


def _fmt_dur(ms):
    if not ms:
        return "-"
    s = int(ms / 1000)
    return f"{s // 60}m{s % 60:02d}s"


# =========================================================================
# Dashboard Screen
# =========================================================================


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("q", "switch_queue", "Queue"),
        Binding("a", "switch_add", "Add Jobs"),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }
    #dash-summary {
        height: 4;
        padding: 0 2;
    }
    #dash-summary Label {
        margin: 0 1;
    }
    #dash-body {
        layout: horizontal;
    }
    #dash-activity {
        width: 2fr;
        border: round $primary;
        padding: 0 1;
    }
    #dash-boards {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    .section-title {
        text-style: bold;
        padding: 1 0 0 0;
    }
    .status-count {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="dash-summary"):
            yield Label("Loading...", id="dash-counts")
        with Horizontal(id="dash-body"):
            with VerticalScroll(id="dash-activity"):
                yield Static("Recent Activity", classes="section-title")
                yield Static("Loading...", id="activity-feed")
            with VerticalScroll(id="dash-boards"):
                yield Static("Board Breakdown", classes="section-title")
                yield Static("Loading...", id="board-breakdown")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(10.0, self._refresh_data)

    @work(thread=True, exclusive=True, group="dash-refresh")
    def _refresh_data(self) -> None:
        conn = _get_conn()
        try:
            status_counts = get_status_counts(conn)
            board_counts = get_board_counts(conn)
            recent = _get_recent_events(conn, limit=30)
            summary = get_summary_stats(conn)
        finally:
            conn.close()
        self.app.call_from_thread(self._apply_data, status_counts, board_counts, recent, summary)

    def _apply_data(
        self,
        status_counts: dict[str, int],
        board_counts: dict[str, dict[str, int]],
        recent: list[dict],
        summary: dict | None = None,
    ) -> None:
        total = sum(status_counts.values())
        submitted = status_counts.get("submitted", 0)
        processing = sum(status_counts.get(s, 0) for s in _PROCESSING_STATUSES)
        queued = status_counts.get("queued", 0)
        drafts = status_counts.get("draft", 0)
        stopped = status_counts.get("stopped", 0)
        attention = sum(status_counts.get(s, 0) for s in _ATTENTION_STATUSES)

        # Worker status
        if hasattr(self.app, "_is_worker_running") and self.app._is_worker_running():
            worker_label = "[green]Workers: ON[/]"
        else:
            worker_label = "[red]Workers: OFF[/]"

        line1 = (
            f"{worker_label}  |  Total: {total}  |  "
            f"[green]Submitted: {submitted}[/]  |  [cyan]Processing: {processing}[/]  |  "
            f"Queued: {queued}  |  [dark_orange]Drafts: {drafts}[/]"
        )
        line2 = f"[red]Stopped: {stopped}[/]  |  [magenta]Attention: {attention}[/]"
        if summary:
            avg_err = summary.get("avg_error_rate", 0)
            avg_dur = summary.get("avg_duration_ms", 0)
            line2 += f"  |  Error Rate: {avg_err * 100:.0f}%  |  Avg: {_fmt_dur(avg_dur)}"
        self.query_one("#dash-counts", Label).update(f"{line1}\n{line2}")

        # Activity feed
        if recent:
            lines = []
            for ev in recent:
                ts = _format_ts(ev.get("created_at"))
                company = ev.get("company") or "?"
                role = ev.get("role_title") or "?"
                etype = ev.get("event_type", "")
                detail = ev.get("detail") or ""
                lines.append(f"[dim]{ts}[/]  {etype:20s}  {company}/{role}  {detail}")
            self.query_one("#activity-feed", Static).update("\n".join(lines))
        else:
            self.query_one("#activity-feed", Static).update("[dim]No recent activity[/]")

        # Board breakdown
        if board_counts:
            lines = []
            for board, statuses in sorted(board_counts.items()):
                board_total = sum(statuses.values())
                board_submitted = statuses.get("submitted", 0)
                pct = int(board_submitted / board_total * 100) if board_total else 0
                bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                lines.append(f"{board:15s}  [{bar}]  {board_submitted}/{board_total}  ({pct}%)")
            self.query_one("#board-breakdown", Static).update("\n".join(lines))
        else:
            self.query_one("#board-breakdown", Static).update("[dim]No jobs yet[/]")

    def action_switch_queue(self) -> None:
        self.app.switch_mode("queue")

    def action_switch_add(self) -> None:
        self.app.switch_mode("add")


# =========================================================================
# Queue Screen
# =========================================================================


class QueueScreen(Screen):
    BINDINGS = [
        Binding("d", "switch_dash", "Dashboard"),
        Binding("a", "switch_add", "Add Jobs"),
        Binding("r", "retry_job", "Retry"),
        Binding("s", "skip_job", "Skip"),
        Binding("p", "prioritize_job", "Prioritize"),
        Binding("delete", "delete_job", "Delete"),
        Binding("slash", "focus_search", "Search"),
    ]

    DEFAULT_CSS = """
    QueueScreen {
        layout: vertical;
    }
    #queue-filters {
        height: 3;
        padding: 0 1;
        layout: horizontal;
    }
    #queue-filters Select {
        width: 20;
        margin: 0 1;
    }
    #queue-filters Input {
        width: 30;
    }
    #queue-filters Button {
        margin: 0 1;
        min-width: 12;
    }
    #queue-table {
        height: 1fr;
    }
    """

    # Cached data
    _cached_jobs: list[dict] = []
    _search_timer: Timer | None = None
    _filter_status: str | None = None
    _filter_board: str | None = None
    _filter_search: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="queue-filters"):
            yield Select(
                [("All Statuses", "")] + [(s, s) for s in JOB_STATUSES],
                value="",
                id="filter-status",
                prompt="Status",
            )
            yield Select(
                [("All Boards", "")],
                value="",
                id="filter-board",
                prompt="Board",
            )
            yield Button("Drafts", variant="warning", id="btn-filter-drafts")
            yield Input(placeholder="Search company/role...", id="filter-search")
        yield DataTable(id="queue-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Status", "Company", "Role", "Board", "Source", "Provider", "Entered UTC")
        self._refresh_data()
        self.set_interval(10.0, self._refresh_data)
        table.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-filter-drafts":
            # Toggle: if already filtering drafts, clear the filter
            if self._filter_status == "draft":
                self._filter_status = None
                self.query_one("#filter-status", Select).value = ""
            else:
                self._filter_status = "draft"
                self.query_one("#filter-status", Select).value = "draft"
            self._refresh_data()

    def on_select_changed(self, event: Select.Changed) -> None:
        val = event.value
        # Select.BLANK / NoSelection is not a str — treat as None
        if not isinstance(val, str) or not val:
            val = None
        if event.select.id == "filter-status":
            self._filter_status = val
        elif event.select.id == "filter-board":
            self._filter_board = val
        self._refresh_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-search":
            # Debounce: cancel pending timer, start new 200ms timer
            if self._search_timer is not None:
                self._search_timer.stop()
            self._filter_search = event.value if event.value.strip() else None
            self._search_timer = self.set_timer(0.2, self._refresh_data)

    @work(thread=True, exclusive=True, group="queue-refresh")
    def _refresh_data(self) -> None:
        conn = _get_conn()
        try:
            jobs = query_jobs(
                conn,
                status=self._filter_status,
                board=self._filter_board,
                search=self._filter_search,
                limit=500,
            )
            # Also get board list for the filter dropdown
            board_counts = get_board_counts(conn)
        finally:
            conn.close()
        self.app.call_from_thread(self._apply_data, jobs, list(board_counts.keys()))

    def _apply_data(self, jobs: list[dict], boards: list[str]) -> None:
        self._cached_jobs = jobs

        # Update board filter options (only if changed)
        board_select = self.query_one("#filter-board", Select)
        new_options = [("All Boards", "")] + [(b, b) for b in sorted(boards)]
        board_select.set_options(new_options)

        table = self.query_one("#queue-table", DataTable)
        # Remember cursor position
        try:
            cursor_row = table.cursor_row
        except Exception:
            cursor_row = 0

        new_rows = []
        for job in jobs:
            status = job.get("status", "")
            color = _STATUS_COLORS.get(status, "white")
            progress_text = job.get("progress") or ""
            status_display = f"[{color}]{status}[/]"
            if progress_text:
                status_display = f"[{color}]{status}[/] {progress_text[:35]}"
            new_rows.append(
                (
                    str(job.get("id", "")),
                    status_display,
                    job.get("company") or "",
                    job.get("role_title") or "",
                    job.get("board") or "",
                    job.get("source") or "",
                    job.get("provider") or "",
                    _format_ts(job.get("status_entered_at") or job.get("queue_timestamp")),
                )
            )

        # Skip repaint if nothing changed
        if hasattr(self, "_last_rows") and self._last_rows == new_rows:
            return
        self._last_rows = new_rows

        table.clear()
        for row in new_rows:
            table.add_row(*row, key=row[0])
        if new_rows and cursor_row < len(new_rows):
            table.move_cursor(row=cursor_row)

    def _get_selected_job_id(self) -> int | None:
        table = self.query_one("#queue-table", DataTable)
        try:
            row_key = table.get_row_at(table.cursor_row)
            return int(row_key[0])
        except Exception:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            row_data = self.query_one("#queue-table", DataTable).get_row(event.row_key)
            job_id = int(row_data[0])
            self.app.push_screen(JobDetailScreen(job_id))
        except Exception:
            pass

    def action_retry_job(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        conn = _get_conn()
        try:
            job = get_job(conn, job_id)
            if job and job["status"] in ("stopped", "needs_board_url"):
                action_detail_json, action_process_info = _tui_action_audit("queue", "retry")
                update_status(
                    conn,
                    job_id,
                    "queued",
                    error_message="",
                    clear_provider=True,
                    retry_after=RETRY_AFTER_SENTINEL,
                    initiator="tui",
                    process_info=action_process_info,
                    event_detail_json=action_detail_json,
                )
                self.notify(f"Job {job_id} re-queued for retry")
            else:
                self.notify(
                    f"Job {job_id} cannot be retried (status: {job['status'] if job else 'unknown'})",
                    severity="warning",
                )
        finally:
            conn.close()
        self._refresh_data()

    def action_skip_job(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("queue", "skip")
            update_status(
                conn,
                job_id,
                "stopped",
                error_message="Manually skipped via TUI",
                failure_type="user_rejected",
                initiator="tui",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            self.notify(f"Job {job_id} skipped")
        finally:
            conn.close()
        self._refresh_data()

    def action_prioritize_job(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        conn = _get_conn()
        try:
            job = get_job(conn, job_id)
            if job:
                new_priority = job["priority"] + 5
                conn.execute("UPDATE jobs SET priority = ? WHERE id = ?", (new_priority, job_id))
                conn.commit()
                self.notify(f"Job {job_id} priority bumped to {new_priority}")
        finally:
            conn.close()
        self._refresh_data()

    def action_delete_job(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM fix_attempts WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM provider_runs WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job_phase_durations WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM field_corrections WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job_metrics WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            self.notify(f"Job {job_id} deleted")
        finally:
            conn.close()
        self._refresh_data()

    def action_focus_search(self) -> None:
        self.query_one("#filter-search", Input).focus()

    def action_switch_dash(self) -> None:
        self.app.switch_mode("dashboard")

    def action_switch_add(self) -> None:
        self.app.switch_mode("add")


# =========================================================================
# Add Jobs Screen
# =========================================================================


def _format_saved_portal_import_feedback(label: str, result: dict) -> str:
    status = str(result.get("status") or "unknown")
    added = int(result.get("added") or 0)
    duplicates = int(result.get("duplicates") or 0)
    unresolved = int(result.get("skipped_unresolved") or 0)
    errors = int(result.get("errors") or 0)
    scraped = int(result.get("scraped") or 0)
    resolved = int(result.get("resolved") or 0)
    message = str(result.get("message") or "").strip()

    line = f"{label}: {added} added"
    if duplicates:
        line += f", {duplicates} duplicates"
    if unresolved:
        line += f", {unresolved} unresolved"
    if errors:
        line += f", {errors} error{'s' if errors != 1 else ''}"
    line += f" ({scraped} scraped, {resolved} resolved, status={status})"
    if message:
        line += f"; {escape(message)}"

    if status == "ok" and (unresolved or errors):
        color = "yellow"
    elif status == "ok":
        color = "green"
    elif status == "auth_required":
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{line}[/]"


class AddJobsScreen(Screen):
    _saved_portal_import_running = False

    BINDINGS = [
        Binding("ctrl+s", "submit_jobs", "Add Jobs", priority=True),
        Binding("ctrl+l", "clear_input", "Clear", priority=True),
        Binding("escape", "switch_queue", "Back", priority=True),
        Binding("f1", "cycle_provider", "Provider", priority=True),
        Binding("f2", "cycle_priority", "Priority", priority=True),
    ]

    DEFAULT_CSS = """
    AddJobsScreen {
        layout: vertical;
        padding: 1 2;
    }
    #add-form {
        height: 1fr;
    }
    #url-input {
        height: 1fr;
        min-height: 8;
    }
    #add-options {
        height: 5;
        layout: horizontal;
        padding: 1 0;
    }
    #add-options Select {
        width: 25;
        margin: 0 1;
    }
    #add-actions {
        height: 3;
        layout: horizontal;
    }
    #add-feedback {
        height: 3;
        padding: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="add-form"):
            yield Static("Paste job URLs (one per line):", classes="section-title")
            yield TextArea(id="url-input")
            with Horizontal(id="add-options"):
                yield Select(
                    [("Auto (fallback chain)", "")] + [(p, p) for p in VALID_PROVIDERS],
                    value="",
                    id="provider-select",
                    prompt="Provider",
                )
                yield Select(
                    [("Normal (0)", "0"), ("High (5)", "5"), ("Urgent (10)", "10")],
                    value="0",
                    id="priority-select",
                    prompt="Priority",
                )
            with Horizontal(id="add-actions"):
                yield Button("Add Jobs", variant="primary", id="btn-add")
                for spec in SAVED_PORTAL_SPECS:
                    suffix = {
                        "linkedin": "Saved",
                        "trueup": "My Jobs",
                        "jackandjill": "Opportunities",
                    }.get(spec.key, "")
                    label = f"Import {spec.label} {suffix}".strip()
                    yield Button(label, variant="default", id=f"btn-import-{spec.key}")
                yield Button("Clear", variant="default", id="btn-clear")
            yield Static("", id="add-feedback")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add":
            self._add_jobs()
        elif event.button.id and event.button.id.startswith("btn-import-"):
            self._import_saved_portal(event.button.id.removeprefix("btn-import-"))
        elif event.button.id == "btn-clear":
            self.query_one("#url-input", TextArea).clear()
            self.query_one("#add-feedback", Static).update("")

    def _add_jobs(self) -> None:
        text = self.query_one("#url-input", TextArea).text.strip()
        if not text:
            self.query_one("#add-feedback", Static).update("[red]No URLs entered[/]")
            return

        # Parse URLs: one per line, or comma-separated
        raw_urls = []
        for line in text.splitlines():
            for part in line.split(","):
                url = part.strip()
                if url and url.startswith("http"):
                    raw_urls.append(url)

        if not raw_urls:
            self.query_one("#add-feedback", Static).update("[red]No valid URLs found[/]")
            return

        provider_val = self.query_one("#provider-select", Select).value
        provider = provider_val if isinstance(provider_val, str) and provider_val else None
        priority_val = self.query_one("#priority-select", Select).value
        priority = int(priority_val) if isinstance(priority_val, str) and priority_val else 0

        conn = _get_conn()
        added = 0
        duplicates = 0
        errors = 0
        try:
            for url in raw_urls:
                try:
                    job_id = add_job(conn, url, priority=priority, provider=provider)
                    if job_id < 0:
                        duplicates += 1
                    else:
                        added += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
                except Exception:
                    errors += 1
                    log.exception("Failed to add job URL in TUI: %s", url)
        finally:
            conn.close()

        summary_color = "green" if added else "yellow" if duplicates and not errors else "red" if errors else "green"
        feedback = f"[{summary_color}]{added} job(s) added[/]"
        if duplicates:
            feedback += f"  [yellow]{duplicates} duplicate(s) skipped[/]"
        if errors:
            feedback += f"  [red]{errors} error{'s' if errors != 1 else ''}[/]"
        self.query_one("#add-feedback", Static).update(feedback)
        if not errors:
            self.query_one("#url-input", TextArea).clear()

    def _selected_provider_priority(self) -> tuple[str | None, int]:
        provider_val = self.query_one("#provider-select", Select).value
        provider = provider_val if isinstance(provider_val, str) and provider_val else None
        priority_val = self.query_one("#priority-select", Select).value
        priority = int(priority_val) if isinstance(priority_val, str) and priority_val else 0
        return provider, priority

    def _set_saved_portal_buttons_disabled(self, disabled: bool) -> None:
        for spec in SAVED_PORTAL_SPECS:
            self.query_one(f"#btn-import-{spec.key}", Button).disabled = disabled

    def _finish_saved_portal_import(self, message: str) -> None:
        self._saved_portal_import_running = False
        self._set_saved_portal_buttons_disabled(False)
        self.query_one("#add-feedback", Static).update(message)

    def _import_saved_portal(self, portal: str) -> None:
        if self._saved_portal_import_running:
            self.query_one("#add-feedback", Static).update("[yellow]Saved portal import already running[/]")
            return

        feedback = self.query_one("#add-feedback", Static)
        spec = saved_portal_import.get_saved_portal(portal)
        label = spec.label
        provider, priority = self._selected_provider_priority()
        self._saved_portal_import_running = True
        self._set_saved_portal_buttons_disabled(True)
        feedback.update(f"[yellow]Importing {label} saved jobs...[/]")
        self._run_saved_portal_import(portal, label, provider, priority)

    @work(thread=True)
    def _run_saved_portal_import(
        self,
        portal: str,
        label: str,
        provider: str | None,
        priority: int,
    ) -> None:
        try:
            module = saved_portal_import.load_saved_portal_module(portal)
            conn = _get_conn()
            try:
                result = module.import_saved_jobs(conn, priority=priority, provider=provider)
            finally:
                conn.close()

            msg = _format_saved_portal_import_feedback(label, result)
        except Exception as exc:
            msg = f"[red]{label} import failed: {escape(str(exc))}[/]"
        self.app.call_from_thread(self._finish_saved_portal_import, msg)

    def action_submit_jobs(self) -> None:
        """Ctrl+S: Add jobs from the URL input."""
        self._add_jobs()

    def action_clear_input(self) -> None:
        """Ctrl+L: Clear the URL input and feedback."""
        self.query_one("#url-input", TextArea).clear()
        self.query_one("#add-feedback", Static).update("")

    def action_cycle_provider(self) -> None:
        """F1: Cycle through provider options."""
        sel = self.query_one("#provider-select", Select)
        options = [""] + list(VALID_PROVIDERS)
        current = sel.value if isinstance(sel.value, str) else ""
        idx = (options.index(current) + 1) % len(options) if current in options else 0
        sel.value = options[idx]
        label = options[idx] or "Auto"
        self.notify(f"Provider: {label}")

    def action_cycle_priority(self) -> None:
        """F2: Cycle through priority levels."""
        sel = self.query_one("#priority-select", Select)
        options = [("Normal", "0"), ("High", "5"), ("Urgent", "10")]
        current = sel.value if isinstance(sel.value, str) else "0"
        current_idx = next((i for i, (_, v) in enumerate(options) if v == current), 0)
        next_idx = (current_idx + 1) % len(options)
        sel.value = options[next_idx][1]
        self.notify(f"Priority: {options[next_idx][0]}")

    def action_switch_dash(self) -> None:
        self.app.switch_mode("dashboard")

    def action_switch_queue(self) -> None:
        self.app.switch_mode("queue")


# =========================================================================
# Job Detail Screen
# =========================================================================


class JobDetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "retry_job", "Retry"),
        Binding("s", "skip_job", "Skip"),
        Binding("p", "prioritize_job", "Prioritize"),
    ]

    DEFAULT_CSS = """
    JobDetailScreen {
        layout: vertical;
    }
    #detail-header {
        height: auto;
        max-height: 10;
        padding: 1 2;
        border: round $primary;
        margin: 0 1;
    }
    #detail-body {
        layout: horizontal;
        height: 1fr;
    }
    #detail-timeline {
        width: 1fr;
        border: round $primary;
        margin: 0 1;
        padding: 0 1;
    }
    #detail-tabs {
        width: 2fr;
        margin: 0 1;
    }
    #draft-actions {
        height: 3;
        layout: horizontal;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #draft-actions Button {
        margin: 0 1;
    }
    #board-url-bar {
        height: 3;
        layout: horizontal;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #board-url-bar Input {
        width: 1fr;
    }
    #board-url-bar Button {
        margin: 0 1;
        min-width: 16;
    }
    .draft-banner {
        text-style: bold;
        background: $warning-darken-3;
        color: $text;
        padding: 0 2;
        text-align: center;
    }
    """

    def __init__(self, job_id: int) -> None:
        super().__init__()
        self.job_id = job_id
        self._job: dict | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading...", id="detail-header")
        # Board URL bar — shown only for needs_board_url status
        with Horizontal(id="board-url-bar"):
            yield Input(placeholder="Paste the direct board URL (Greenhouse, Ashby, Lever, etc.)", id="board-url-input")
            yield Button("Set Board URL", variant="primary", id="btn-set-board-url")
        # Draft action bar — hidden initially, shown only for draft status
        with Horizontal(id="draft-actions"):
            yield Button("Approve + Submit", variant="success", id="btn-draft-approve")
            yield Button("Reject", variant="error", id="btn-draft-reject")
            yield Button("Reset to New", variant="warning", id="btn-draft-reset-to-new")
            yield Button("Regenerate", variant="warning", id="btn-draft-regenerate")
            yield Button("Edit in $EDITOR", variant="default", id="btn-draft-edit")
            yield Button("Open PNG", variant="default", id="btn-draft-open-png")
        with Horizontal(id="detail-body"):
            with VerticalScroll(id="detail-timeline"):
                yield Static("Timeline", classes="section-title")
                yield Static("Loading...", id="timeline-content")
            with TabbedContent(id="detail-tabs"):
                with TabPane("Draft", id="tab-draft"):
                    yield Static("", id="draft-content")
                with TabPane("Attention", id="tab-attention"):
                    yield Static("", id="attention-content")
                with TabPane("Report", id="tab-report"):
                    yield Static("", id="report-content")
                with TabPane("Screenshot", id="tab-screenshot"):
                    yield Static("", id="screenshot-content")
                with TabPane("Recording", id="tab-recording"):
                    yield Static("", id="recording-content")
                with TabPane("Resume", id="tab-resume"):
                    yield Static("", id="resume-content")
                with TabPane("Cover Letter", id="tab-cover-letter"):
                    yield Static("", id="cover-letter-content")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(10.0, self._refresh_data)

    @work(thread=True, exclusive=True, group="detail-refresh")
    def _refresh_data(self) -> None:
        conn = _get_conn()
        try:
            job = get_job(conn, self.job_id)
            try:
                timeline = get_job_timeline(conn, self.job_id)
            except sqlite3.DatabaseError:
                log.warning("Timeline query failed for job %d", self.job_id, exc_info=True)
                timeline = []
        finally:
            conn.close()
        self.app.call_from_thread(self._apply_data, job, timeline)

    @staticmethod
    def _read_progress(output_dir: str) -> str | None:
        """Read .progress.json from the output dir and format as a status line."""
        import json as _json

        try:
            progress_path = Path(output_dir) / ".progress.json"
            if not progress_path.exists():
                return None
            data = _json.loads(progress_path.read_text(encoding="utf-8"))
            pct = data.get("pct", 0)
            detail = data.get("detail", "")
            bar_len = pct // 5
            filled = "█" * bar_len
            empty = "░" * (20 - bar_len)
            return f"Progress: [green]{filled}[/green]{empty} {pct}%  {detail}"
        except Exception:
            return None

    def _apply_data(self, job: dict | None, timeline: list[dict]) -> None:
        if not job:
            self.query_one("#detail-header", Static).update("[red]Job not found[/]")
            return

        self._job = job
        status = job.get("status", "")
        color = _STATUS_COLORS.get(status, "white")
        is_draft = status == "draft"

        header_lines = [
            f"[bold]{job.get('company') or '?'}[/] — {job.get('role_title') or '?'}",
            f"Status: [{color}]{status}[/]    Board: {job.get('board') or '?'}    "
            f"Source: {job.get('source') or '?'}    Provider: {job.get('provider') or 'auto'}",
        ]
        if job.get("status_entered_at"):
            header_lines.append(f"Status since: {_format_ts(job.get('status_entered_at'))}")
        if job.get("url"):
            header_lines.append(f"URL: {job['url']}")
        if job.get("board_url") and job["board_url"] != job.get("url"):
            header_lines.append(f"Board URL: {job['board_url']}")
        if job.get("canonical_url") and job["canonical_url"] != job.get("board_url"):
            header_lines.append(f"Canonical: {job['canonical_url']}")
        if job.get("output_dir"):
            header_lines.append(f"Output: {job['output_dir']}")
        if job.get("notion_url"):
            header_lines.append(f"Notion: {job['notion_url']}")
        if job.get("error_message"):
            err_escaped = job["error_message"].replace("[", r"\[")
            header_lines.append(f"[red]Error: {err_escaped}[/]")

        # Show pipeline progress from DB
        if job.get("progress"):
            prog_escaped = job["progress"].replace("[", r"\[")
            header_lines.append(f"[cyan]Progress: {prog_escaped}[/]")
        # Also check .progress.json for detailed step info
        if status in ("generating", "resolving", "submitting") and job.get("output_dir"):
            progress_line = self._read_progress(job["output_dir"])
            if progress_line:
                header_lines.append(progress_line)

        self.query_one("#detail-header", Static).update("\n".join(header_lines))

        # Show/hide board URL bar for needs_board_url jobs
        board_url_bar = self.query_one("#board-url-bar")
        board_url_bar.display = status == "needs_board_url"

        # Show/hide draft action bar
        draft_actions = self.query_one("#draft-actions")
        draft_actions.display = is_draft

        # Auto-select Draft tab for draft jobs on first load only
        if is_draft and not hasattr(self, "_draft_tab_set"):
            self._draft_tab_set = True
            try:
                tabs = self.query_one("#detail-tabs", TabbedContent)
                tabs.active = "tab-draft"
            except Exception:
                pass

        # Timeline
        if timeline:
            lines = []
            for ev in timeline:
                ts = _format_ts(ev.get("created_at"))
                etype = ev.get("event_type", "")
                detail = ev.get("detail") or ""
                lines.append(f"[dim]{ts}[/]  {etype}  {detail}")
            self.query_one("#timeline-content", Static).update("\n".join(lines))
        else:
            self.query_one("#timeline-content", Static).update("[dim]No events yet[/]")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Lazy-load tab content when a tab is selected."""
        tab_id = event.pane.id
        if tab_id == "tab-draft":
            self._load_draft()
        elif tab_id == "tab-attention":
            self._load_attention()
        elif tab_id == "tab-report":
            self._load_report()
        elif tab_id == "tab-screenshot":
            self._load_screenshot()
        elif tab_id == "tab-recording":
            self._load_recording()
        elif tab_id == "tab-resume":
            self._load_resume()
        elif tab_id == "tab-cover-letter":
            self._load_cover_letter()

    @work(thread=True, exclusive=True, group="tab-load-draft")
    def _load_draft(self) -> None:
        """Load draft_summary.md content and image path info."""
        if not self._job or not self._job.get("output_dir"):
            self.app.call_from_thread(
                self.query_one("#draft-content", Static).update,
                "[dim]No draft summary available (no output directory)[/]",
            )
            return

        out_dir = Path(self._job["output_dir"])
        lines: list[str] = []
        proof = resolve_current_submit_artifacts(out_dir, board_name=self._job.get("board"))
        review_state = draft_review_state(out_dir, board_name=self._job.get("board"))

        # Draft status
        status_path = out_dir / "draft_status.json"
        if status_path.exists():
            try:
                import json

                ds = json.loads(status_path.read_text(encoding="utf-8"))
                draft_st = ds.get("status", "?")
                version = ds.get("draft_version", "?")
                created = ds.get("created_at", "?")
                lines.append(f"[bold]Draft Status:[/] {draft_st}  |  Version: {version}  |  Created: {created}")
                lines.append("")
            except Exception:
                pass
        if review_state:
            lines.append(
                f"[bold]Draft Review State:[/] {review_state.get('state', 'unknown')}  |  "
                f"{review_state.get('reason', 'Not recorded')}"
            )
            lines.append("")

        # Draft summary PNG path
        png_path = out_dir / "draft_summary.png"
        if png_path.exists():
            lines.append(f"[bold]Summary Image:[/] {png_path}")
            lines.append("[dim]Use 'Open PNG' button to view externally[/]")
            lines.append("")

        # Pre-submit screenshot path
        submit_dir = role_submit_dir(out_dir)
        pre_submit_path = proof.get("pre_submit_screenshot")
        if pre_submit_path:
            lines.append(f"[bold]Pre-submit Screenshot:[/] {pre_submit_path}")
        review_screenshot_path = proof.get("review_screenshot")
        if review_screenshot_path and review_screenshot_path != pre_submit_path:
            lines.append(f"[bold]Review Screenshot:[/] {review_screenshot_path}")
        elif submit_dir.is_dir():
            for pattern in ("*_pre_submit.png", "pre_submit_screenshot.png"):
                for img in submit_dir.glob(pattern):
                    lines.append(f"[bold]Pre-submit Screenshot:[/] {img}")
                    break

        if lines:
            lines.append("")
            lines.append("---")
            lines.append("")

        # Draft summary markdown content
        summary_path = out_dir / "draft_summary.md"
        if summary_path.exists():
            try:
                md_text = summary_path.read_text(encoding="utf-8")[:10000]
                lines.append(md_text)
                if len(md_text) >= 10000:
                    lines.append("\n[dim]... truncated ...[/]")
            except Exception as e:
                lines.append(f"[red]Error reading draft_summary.md: {e}[/]")
        else:
            lines.append("[dim]No draft_summary.md found[/]")

        display = "\n".join(lines) if lines else "[dim]No draft information available[/]"
        self.app.call_from_thread(self.query_one("#draft-content", Static).update, display)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle action buttons."""
        if event.button.id == "btn-set-board-url":
            self._set_board_url()
        elif event.button.id == "btn-draft-approve":
            self._draft_approve()
        elif event.button.id == "btn-draft-reject":
            self._draft_reject()
        elif event.button.id == "btn-draft-reset-to-new":
            self._draft_reset_to_new()
        elif event.button.id == "btn-draft-regenerate":
            self._draft_regenerate()
        elif event.button.id == "btn-draft-edit":
            self._draft_edit()
        elif event.button.id == "btn-draft-open-png":
            self._draft_open_png()

    @work(thread=True, exclusive=True, group="board-url-action")
    def _set_board_url(self) -> None:
        """Set the board URL for a needs_board_url job and re-queue it."""
        if not self._job:
            return
        board_url = self.query_one("#board-url-input", Input).value.strip()
        if not board_url or not board_url.startswith("http"):
            self.app.call_from_thread(self.notify, "Please enter a valid URL", severity="warning")
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "set-board-url")
            update_status(
                conn,
                self.job_id,
                "queued",
                board_url=board_url,
                canonical_url=board_url,
                error_message=None,
                initiator="tui",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            log_event(
                conn,
                self.job_id,
                "board_url_set_manually",
                detail=board_url,
                detail_json=action_detail_json,
                initiator="tui",
                process_info=action_process_info,
            )
        finally:
            conn.close()
        self.app.call_from_thread(self.notify, "Board URL set — job re-queued")
        self.app.call_from_thread(self.query_one("#board-url-input", Input).clear)
        self._refresh_data()

    @work(thread=True, exclusive=True, group="draft-action")
    def _draft_approve(self) -> None:
        """Approve the draft and transition to submitting."""
        if not self._job:
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "approve")
            ok = approve_job(
                conn,
                self.job_id,
                initiator="tui",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            )
            if ok:
                ensure_job_metrics(conn, self.job_id)
                m = get_job_metrics(conn, self.job_id)
                if m:
                    update_job_metrics(conn, self.job_id, manual_interventions=m["manual_interventions"] + 1)
        finally:
            conn.close()
        if ok:
            self.app.call_from_thread(self.notify, f"Job {self.job_id} approved -- queued for submission")
        else:
            conn = _get_conn()
            try:
                message = approve_job_failure_message(conn, self.job_id)
            finally:
                conn.close()
            self.app.call_from_thread(self.notify, message, severity="warning")
        self._refresh_data()

    @work(thread=True, exclusive=True, group="draft-action")
    def _draft_reject(self) -> None:
        """Reject the draft — set status to stopped."""
        if not self._job:
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "reject")
            update_status(
                conn,
                self.job_id,
                "stopped",
                error_message="Draft rejected via TUI",
                failure_type="user_rejected",
                initiator="tui",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            ensure_job_metrics(conn, self.job_id)
            m = get_job_metrics(conn, self.job_id)
            if m:
                update_job_metrics(conn, self.job_id, manual_interventions=m["manual_interventions"] + 1)
        finally:
            conn.close()
        self.app.call_from_thread(self.notify, f"Job {self.job_id} rejected -- set to stopped")
        self._refresh_data()

    @work(thread=True, exclusive=True, group="draft-action")
    def _draft_reset_to_new(self) -> None:
        """Reset the job to the newly-added queued state."""
        if not self._job:
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "reset-to-new")
            ok = reset_job_to_new(
                conn,
                self.job_id,
                initiator="tui",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            )
            if ok:
                ensure_job_metrics(conn, self.job_id)
                m = get_job_metrics(conn, self.job_id)
                if m:
                    update_job_metrics(conn, self.job_id, manual_interventions=m["manual_interventions"] + 1)
        finally:
            conn.close()
        if ok:
            self.app.call_from_thread(self.notify, f"Job {self.job_id} reset to newly added and re-queued")
        else:
            self.app.call_from_thread(
                self.notify, f"Job {self.job_id} could not be reset to new", severity="warning"
            )
        self._refresh_data()

    @work(thread=True, exclusive=True, group="draft-action")
    def _draft_regenerate(self) -> None:
        """Regenerate the draft — transition back to generating."""
        if not self._job:
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "regenerate")
            ok = regenerate_job(
                conn,
                self.job_id,
                initiator="tui",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            )
            if ok:
                ensure_job_metrics(conn, self.job_id)
                m = get_job_metrics(conn, self.job_id)
                if m:
                    update_job_metrics(conn, self.job_id, manual_interventions=m["manual_interventions"] + 1)
        finally:
            conn.close()
        if ok:
            self.app.call_from_thread(self.notify, f"Job {self.job_id} queued for regeneration")
        else:
            self.app.call_from_thread(
                self.notify, f"Job {self.job_id} could not be regenerated (not in draft status)", severity="warning"
            )
        self._refresh_data()

    def _draft_edit(self) -> None:
        """Open draft_summary.md in $EDITOR."""
        if not self._job or not self._job.get("output_dir"):
            self.notify("No output directory", severity="warning")
            return
        summary_path = Path(self._job["output_dir"]) / "draft_summary.md"
        if not summary_path.exists():
            self.notify("draft_summary.md not found", severity="warning")
            return
        editor = os.environ.get("EDITOR", "vim")
        try:
            # Suspend the TUI and open the editor
            with self.app.suspend():
                subprocess.run([editor, str(summary_path)])
            self.notify("Editor closed -- reloading draft")
            self._load_draft()
        except Exception as e:
            self.notify(f"Failed to open editor: {e}", severity="error")

    def _draft_open_png(self) -> None:
        """Open draft_summary.png externally."""
        if not self._job or not self._job.get("output_dir"):
            self.notify("No output directory", severity="warning")
            return
        png_path = Path(self._job["output_dir"]) / "draft_summary.png"
        if not png_path.exists():
            self.notify("draft_summary.png not found", severity="warning")
            return
        # Use platform-appropriate open command
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(png_path)])
        elif platform.system() == "Linux":
            subprocess.Popen(["xdg-open", str(png_path)])
        else:
            subprocess.Popen(["start", str(png_path)], shell=True)
        self.notify(f"Opened {png_path.name}")

    @work(thread=True, exclusive=True, group="tab-load-attention")
    def _load_attention(self) -> None:
        """Load all 'needs attention' items for this job."""
        if not self._job or not self._job.get("output_dir"):
            self.app.call_from_thread(
                self.query_one("#attention-content", Static).update,
                "[dim]No output directory[/]",
            )
            return

        out_dir = Path(self._job["output_dir"])
        items: list[str] = []
        submit_dir = role_submit_dir(out_dir)

        # 1. Pending user input (questions needing human answers)
        pending = load_pending_user_input_for_submit_attempt(out_dir)
        if pending is not None:
            _pending_path, data = pending
            items.append("[bold red]Pending User Input[/]")
            items.append(f"  {data.get('message', '')}")
            for q in data.get("questions", []):
                label = q.get("label") or q.get("field_name", "?")
                planned = q.get("planned_value", "")
                items.append(f"  [yellow]Q:[/] {label}")
                if planned:
                    items.append(f"    Planned: {planned}")
                if q.get("artifact_key"):
                    items.append(f"    Artifact: {q.get('artifact_key')}")
                if q.get("reason"):
                    items.append(f"    Reason: {q.get('reason')}")
                elif q.get("note"):
                    items.append(f"    Note: {q.get('note')}")
            items.append("")

        # 2. Auth failures
        for auth_file in ("workday_auth_failure.json", "icims_auth_failure.json"):
            af = submit_dir / auth_file
            if af.exists():
                try:
                    import json

                    data = json.loads(af.read_text(encoding="utf-8"))
                    items.append("[bold red]Auth Failure[/]")
                    items.append(f"  Status: {data.get('status', '?')}")
                    if data.get("auth_state"):
                        items.append(f"  Auth State: {data.get('auth_state')}")
                    if data.get("auth_scope"):
                        items.append(f"  Auth Scope: {data.get('auth_scope')}")
                    if data.get("last_attempted_step"):
                        items.append(f"  Last Step: {data.get('last_attempted_step')}")
                    if data.get("heading_text"):
                        items.append(f"  Heading: {data.get('heading_text')}")
                    if data.get("alert_text"):
                        items.append(f"  Alert: {data.get('alert_text')}")
                    items.append(f"  Message: {data.get('message', '?')}")
                    for action in data.get("visible_actions", [])[:4]:
                        items.append(f"  [cyan]CTA:[/] {action}")
                    for s in data.get("suggestions", []):
                        items.append(f"  [yellow]Suggestion:[/] {s}")
                    items.append("")
                except Exception:
                    pass

        # 3. Captcha skips
        result_file = submit_dir / "application_submission_result.json"
        if result_file.exists():
            try:
                import json

                data = json.loads(result_file.read_text(encoding="utf-8"))
                result = data.get("result", "")
                if "captcha" in result:
                    items.append("[bold yellow]Captcha Required[/]")
                    items.append("  Application skipped due to captcha. Apply manually.")
                    items.append("")
                elif result == "service_unavailable":
                    items.append("[bold yellow]Service Unavailable[/]")
                    items.append(f"  {data.get('message', 'Workday was temporarily unavailable.')}")
                    items.append("")
                elif "auth" in result:
                    items.append("[bold yellow]Auth Skipped[/]")
                    items.append(f"  {data.get('message', 'Authentication failed. Reset password and retry.')}")
                    items.append("")
            except Exception:
                pass

        # 4. Unsupported board
        ub = submit_dir / "unsupported_board.json"
        if ub.exists():
            try:
                import json

                data = json.loads(ub.read_text(encoding="utf-8"))
                items.append("[bold yellow]Unsupported Board[/]")
                items.append(f"  Board: {data.get('board_hint', '?')}")
                items.append(f"  URL: {data.get('url', '?')}")
                items.append("  Apply manually using generated documents.")
                items.append("")
            except Exception:
                pass

        # 5. Explicit unavailable posting
        unavailable = submit_dir / "job_unavailable.json"
        if unavailable.exists():
            try:
                import json

                data = json.loads(unavailable.read_text(encoding="utf-8"))
                items.append("[bold red]Job Unavailable[/]")
                if data.get("message"):
                    items.append(f"  {data.get('message')}")
                if data.get("application_url"):
                    items.append(f"  URL: {data.get('application_url')}")
                items.append("")
            except Exception:
                pass

        # 6. LLM-generated answers (from autofill report — flag non-deterministic ones)
        report = submit_dir / "autofill_report.json"
        resolved_report = resolve_current_submit_artifacts(out_dir, board_name=self._job.get("board")).get(
            "report_json"
        )
        if resolved_report:
            report = Path(resolved_report)
        elif not report.exists():
            for board in ("greenhouse", "phenom", "workday", "ashby", "lever", "gem", "dover", "icims"):
                candidate = submit_dir / f"{board}_autofill_report.json"
                if candidate.exists():
                    report = candidate
                    break
        if report.exists():
            try:
                import json

                data = json.loads(report.read_text(encoding="utf-8"))
                llm_fields = [
                    f
                    for f in data.get("fields", []) + data.get("steps", [])
                    if f.get("source") == "generated_application_answer"
                ]
                if llm_fields:
                    items.append("[bold cyan]LLM-Generated Answers (review for accuracy)[/]")
                    for f in llm_fields:
                        name = f.get("field_name") or f.get("label") or "?"
                        value = str(f.get("value", ""))[:100]
                        items.append(f"  [cyan]{name}:[/] {value}")
                    items.append("")
            except Exception:
                pass

        if not items:
            display = "[green]No items need attention[/]"
        else:
            display = "\n".join(items)

        self.app.call_from_thread(self.query_one("#attention-content", Static).update, display)

    @work(thread=True, exclusive=True, group="tab-load")
    def _load_report(self) -> None:
        proof = self._resolve_submit_artifacts()
        report_path = Path(proof["report_md"]) if proof and proof.get("report_md") else None
        content = self._read_path_text(report_path)
        if not content:
            report_path = Path(proof["report_json"]) if proof and proof.get("report_json") else None
            if report_path and report_path.exists():
                try:
                    content = report_path.read_text(encoding="utf-8")[:10000]
                except Exception:
                    content = None
        if not content:
            content = self._read_output_file("submit", "autofill_report.md")
        if not content:
            content = self._read_output_file("submit", "autofill_report.txt")
        if not content:
            content = "[dim]No report available[/]"
        self.app.call_from_thread(self.query_one("#report-content", Static).update, content)

    @work(thread=True, exclusive=True, group="tab-load")
    def _load_screenshot(self) -> None:
        if not self._job or not self._job.get("output_dir"):
            self.app.call_from_thread(
                self.query_one("#screenshot-content", Static).update,
                "[dim]No screenshot available[/]",
            )
            return
        output_dir = Path(self._job["output_dir"])
        proof = self._resolve_submit_artifacts()
        found = None
        candidates = []
        if proof and proof.get("pre_submit_screenshot"):
            candidates.append(Path(proof["pre_submit_screenshot"]))
        if proof and proof.get("submit_debug_screenshot"):
            candidates.append(Path(proof["submit_debug_screenshot"]))
        candidates.extend(
            [
                output_dir / "submit" / "pre_submit_screenshot.png",
                output_dir / "submit" / "screenshot.png",
            ]
        )
        for candidate in candidates:
            if candidate.exists():
                found = candidate
                break
        if found:
            msg = f"Screenshot: {found}\n\n[dim]Press Enter to open externally[/]"
        else:
            pending = load_pending_user_input_for_submit_attempt(output_dir)
            artifact_reason = None
            artifact_path = None
            if pending is not None:
                for question in pending[1].get("questions", []):
                    if str(question.get("artifact_key") or "").strip() == "pre_submit_screenshot":
                        artifact_reason = str(question.get("reason") or question.get("note") or "").strip()
                        artifact_path = str(question.get("planned_value") or "").strip()
                        break
            if artifact_reason:
                msg = f"[red]{artifact_reason}[/]"
                if artifact_path:
                    msg += f"\n\nExpected: {artifact_path}"
            else:
                msg = "[dim]No screenshot available[/]"
        self.app.call_from_thread(self.query_one("#screenshot-content", Static).update, msg)

    @work(thread=True, exclusive=True, group="tab-load")
    def _load_recording(self) -> None:
        content = self._read_output_file("submit", "debug_recording", "action_log.md")
        if not content:
            content = "[dim]No recording available[/]"
        self.app.call_from_thread(self.query_one("#recording-content", Static).update, content)

    @work(thread=True, exclusive=True, group="tab-load")
    def _load_resume(self) -> None:
        content = self._load_document_tab("Resume")
        self.app.call_from_thread(self.query_one("#resume-content", Static).update, content)

    @work(thread=True, exclusive=True, group="tab-load")
    def _load_cover_letter(self) -> None:
        content = self._load_document_tab("Cover Letter")
        self.app.call_from_thread(self.query_one("#cover-letter-content", Static).update, content)

    def _load_document_tab(self, doc_type: str) -> str:
        """Load document content for Resume or Cover Letter tabs.

        Shows inline content from JSON/TXT source files and auto-opens PDF.
        """
        if not self._job or not self._job.get("output_dir"):
            return f"[dim]No {doc_type.lower()} available[/dim]"

        output_dir = Path(self._job["output_dir"])
        content_dir = output_dir / "content"
        output_dir / "documents"

        lines = []
        is_resume = "resume" in doc_type.lower()

        # Show structured content from source files
        if is_resume:
            src = content_dir / "resume_content.json"
            if src.exists():
                try:
                    import json as _json

                    data = _json.loads(src.read_text(encoding="utf-8"))
                    lines.append(f"[bold]Tagline:[/bold] {data.get('tagline', '-')}")
                    lines.append(f"[bold]Summary:[/bold] {data.get('summary') or '(none)'}")
                    lines.append("")
                    for company, bullets in data.get("positions", {}).items():
                        lines.append(f"[bold]{company.upper()}[/bold]")
                        for i, b in enumerate(bullets, 1):
                            bold = b.get("bold", "")
                            text = b.get("text", "")
                            lines.append(f"  {i}. [bold]{bold}[/bold]{text}")
                        lines.append("")
                except Exception as e:
                    lines.append(f"[red]Error reading resume content: {e}[/red]")
        else:
            src = content_dir / "cover_letter_text.txt"
            if src.exists():
                try:
                    text = src.read_text(encoding="utf-8")
                    lines.append(text[:8000])
                    if len(text) > 8000:
                        lines.append("\n[dim]... truncated ...[/dim]")
                except Exception as e:
                    lines.append(f"[red]Error reading cover letter: {e}[/red]")

        if not lines:
            return f"[dim]No {doc_type.lower()} content found[/dim]"

        return "\n".join(lines)

    def _read_output_file(self, *parts: str) -> str | None:
        """Read a file from the job's output directory."""
        if not self._job or not self._job.get("output_dir"):
            return None
        path = Path(self._job["output_dir"])
        for index, p in enumerate(parts):
            if index == 0 and p == "submit":
                path = role_submit_dir(path)
                continue
            path = path / p
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")[:10000]
            except Exception:
                return None
        return None

    def _resolve_submit_artifacts(self) -> dict[str, object] | None:
        if not self._job or not self._job.get("output_dir"):
            return None
        return resolve_current_submit_artifacts(
            Path(self._job["output_dir"]),
            board_name=self._job.get("board"),
        )

    @staticmethod
    def _read_path_text(path: Path | None) -> str | None:
        if path is None or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")[:10000]
        except Exception:
            return None

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_retry_job(self) -> None:
        if not self._job:
            return
        conn = _get_conn()
        try:
            if self._job["status"] in ("stopped", "needs_board_url"):
                action_detail_json, action_process_info = _tui_action_audit("detail", "retry")
                update_status(
                    conn,
                    self.job_id,
                    "queued",
                    error_message="",
                    clear_provider=True,
                    retry_after=RETRY_AFTER_SENTINEL,
                    initiator="tui",
                    process_info=action_process_info,
                    event_detail_json=action_detail_json,
                )
                self.notify(f"Job {self.job_id} re-queued")
            else:
                self.notify("Cannot retry this job", severity="warning")
        finally:
            conn.close()
        self._refresh_data()

    def action_skip_job(self) -> None:
        if not self._job:
            return
        conn = _get_conn()
        try:
            action_detail_json, action_process_info = _tui_action_audit("detail", "skip")
            update_status(
                conn,
                self.job_id,
                "stopped",
                error_message="Manually skipped via TUI",
                failure_type="user_rejected",
                initiator="tui",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            self.notify(f"Job {self.job_id} skipped")
        finally:
            conn.close()
        self._refresh_data()

    def action_prioritize_job(self) -> None:
        if not self._job:
            return
        conn = _get_conn()
        try:
            new_priority = self._job["priority"] + 5
            conn.execute("UPDATE jobs SET priority = ? WHERE id = ?", (new_priority, self.job_id))
            conn.commit()
            self.notify(f"Job {self.job_id} priority bumped to {new_priority}")
        finally:
            conn.close()
        self._refresh_data()


# =========================================================================
# Help Screen
# =========================================================================

_HELP_TEXT = """\
# Job Application TUI — Keybindings

| Key | Action |
|-----|--------|
| D | Dashboard view |
| Q | Queue view |
| A | Add jobs |
| Enter | Job detail (from queue) |
| Esc | Back |
| R | Retry selected job |
| S | Skip selected job |
| P | Prioritize selected job |
| Delete | Remove job from queue |
| / | Focus search filter |
| ? | This help screen |
| Ctrl+C | Quit TUI |

## Draft Review (Job Detail Screen)

When a job is in **draft** status, the detail view shows:
- **Draft tab** with summary content, status, and image paths
- **Action buttons**: Approve + Submit, Reject, Reset to New, Regenerate, Edit, Open PNG
- Use the **Drafts** button in the Queue filter bar to filter to draft jobs only
"""


class HelpScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("question_mark", "go_back", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-container"):
            yield Markdown(_HELP_TEXT)

    def action_go_back(self) -> None:
        self.app.pop_screen()


# =========================================================================
# Stats Screen
# =========================================================================


class StatsScreen(Screen):
    BINDINGS = [
        Binding("d", "switch_dash", "Dashboard"),
        Binding("q", "switch_queue", "Queue"),
        Binding("a", "switch_add", "Add Jobs"),
    ]

    DEFAULT_CSS = """
    StatsScreen {
        layout: vertical;
    }
    #stats-summary {
        height: 5;
        padding: 1 2;
    }
    #stats-summary Label {
        margin: 0 1;
    }
    #stats-body {
        layout: horizontal;
        height: 1fr;
    }
    #stats-table {
        width: 2fr;
        border: round $primary;
    }
    #stats-sidebar {
        width: 1fr;
        layout: vertical;
    }
    #phase-breakdown {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #board-error-rates {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    .section-title {
        text-style: bold;
        padding: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="stats-summary"):
            yield Label("Loading...", id="stats-counts")
        with Horizontal(id="stats-body"):
            yield DataTable(id="stats-table")
            with Vertical(id="stats-sidebar"):
                with VerticalScroll(id="phase-breakdown"):
                    yield Static("Phase Breakdown", classes="section-title")
                    yield Static("Loading...", id="phase-content")
                with VerticalScroll(id="board-error-rates"):
                    yield Static("Board Error Rates", classes="section-title")
                    yield Static("Loading...", id="board-error-content")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#stats-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "ID",
            "Company",
            "Role",
            "Status",
            "Duration",
            "Corrected/Total",
            "Error %",
            "Fixes",
            "Interventions",
        )
        self._refresh_data()
        self.set_interval(10.0, self._refresh_data)

    @work(thread=True, exclusive=True, group="stats-refresh")
    def _refresh_data(self) -> None:
        conn = _get_conn()
        try:
            processed = get_jobs_processed_counts(conn)
            summary = get_summary_stats(conn)
            all_metrics = get_all_job_metrics(conn)
            phase_durations = get_phase_avg_durations(conn)
            board_errors = get_board_error_rates(conn)
        finally:
            conn.close()
        self.app.call_from_thread(
            self._apply_data,
            processed,
            summary,
            all_metrics,
            phase_durations,
            board_errors,
        )

    def _apply_data(
        self,
        processed: dict,
        summary: dict,
        all_metrics: list[dict],
        phase_durations: dict[str, float],
        board_errors: dict[str, float],
    ) -> None:
        # --- Summary panel ---
        counts_text = (
            f"Processed: "
            f"1h: {processed.get('last_1h', 0)}  "
            f"24h: {processed.get('last_24h', 0)}  "
            f"7d: {processed.get('last_7d', 0)}  "
            f"All: {processed.get('all_time', 0)}"
            f"  |  "
            f"[green]Success: {summary.get('success_rate', 0) * 100:.0f}% ({summary.get('submitted', 0)})[/]"
            f"  "
            f"[red]Fail: {summary.get('failure_rate', 0) * 100:.0f}% ({summary.get('failed', 0)})[/]"
            f"  |  "
            f"Intervention: {summary.get('intervention_rate', 0) * 100:.0f}%"
            f"  |  "
            f"Avg Error: {summary.get('avg_error_rate', 0) * 100:.0f}%"
            f"  |  "
            f"Avg Duration: {_fmt_dur(summary.get('avg_duration_ms', 0))}"
        )
        self.query_one("#stats-counts", Label).update(counts_text)

        # --- Per-job metrics table ---
        table = self.query_one("#stats-table", DataTable)
        try:
            cursor_row = table.cursor_row
        except Exception:
            cursor_row = 0

        table.clear()
        for m in all_metrics:
            status = m.get("status", "")
            color = _STATUS_COLORS.get(status, "white")
            corr = m.get("fields_corrected", 0)
            total = m.get("total_fields", 0)
            err_pct = m.get("field_error_rate", 0)
            table.add_row(
                str(m.get("id", "")),
                m.get("company") or "",
                m.get("role_title") or "",
                f"[{color}]{status}[/]",
                _fmt_dur(m.get("total_duration_ms")),
                f"{corr}/{total}",
                f"{err_pct * 100:.0f}%",
                str(m.get("auto_fix_attempts", 0)),
                str(m.get("manual_interventions", 0)),
                key=str(m.get("id", "")),
            )

        if all_metrics and cursor_row < len(all_metrics):
            table.move_cursor(row=cursor_row)

        # --- Phase breakdown ---
        if phase_durations:
            slowest_phase = max(phase_durations, key=phase_durations.get)
            lines = []
            for phase, avg_ms in sorted(phase_durations.items(), key=lambda x: x[1], reverse=True):
                marker = " [bold red]<< slowest[/]" if phase == slowest_phase else ""
                lines.append(f"  {phase:20s}  {_fmt_dur(avg_ms)}{marker}")
            self.query_one("#phase-content", Static).update("\n".join(lines))
        else:
            self.query_one("#phase-content", Static).update("[dim]No phase data yet[/]")

        # --- Board error rates ---
        if board_errors:
            sorted_boards = sorted(board_errors.items(), key=lambda x: x[1], reverse=True)
            lines = []
            for board, rate in sorted_boards:
                pct = int(rate * 100)
                bar_len = min(pct // 2, 20)
                bar = "#" * bar_len + "-" * (20 - bar_len)
                lines.append(f"  {board:15s}  [{bar}]  {pct}%")
            self.query_one("#board-error-content", Static).update("\n".join(lines))
        else:
            self.query_one("#board-error-content", Static).update("[dim]No board error data yet[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            row_data = self.query_one("#stats-table", DataTable).get_row(event.row_key)
            job_id = int(row_data[0])
            self.app.push_screen(JobDetailScreen(job_id))
        except Exception:
            pass

    def action_switch_dash(self) -> None:
        self.app.switch_mode("dashboard")

    def action_switch_queue(self) -> None:
        self.app.switch_mode("queue")

    def action_switch_add(self) -> None:
        self.app.switch_mode("add")


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("d", "switch_dash", "Dashboard"),
        Binding("q", "switch_queue", "Queue"),
        Binding("a", "switch_add", "Add Jobs"),
        Binding("s", "switch_stats", "Stats"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }
    #settings-scroll {
        padding: 0 1;
    }
    #settings-onboarding,
    #settings-feedback {
        margin: 1 0;
    }
    #settings-material-editor {
        height: 18;
        margin: 1 0;
    }
    .settings-row {
        margin: 0 0 1 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._active_material_key = "master_resume"
        self._material_cache = {value: "" for _, value in _SETTINGS_MATERIAL_KEYS}
        self._material_meta: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="settings-scroll"):
            yield Static("", id="settings-onboarding")
            yield Select(_SETTINGS_MATERIAL_KEYS, value=self._active_material_key, id="settings-material-select")
            yield Input(
                placeholder="Local file path or public Google Drive/Docs URL",
                id="settings-import-source",
                classes="settings-row",
            )
            with Horizontal(classes="settings-row"):
                yield Button("Import Path/URL", id="btn-settings-import")
                yield Button("Save Settings", variant="primary", id="btn-settings-save")
            yield TextArea(id="settings-material-editor")
            yield Select(
                [
                    ("OpenAI", "openai"),
                    ("Gemini", "gemini"),
                    ("Gemini Flash", "gemini-flash"),
                    ("Codex", "codex"),
                    ("Claude", "claude"),
                ],
                value="openai",
                id="settings-default-provider",
            )
            yield Input(placeholder="openai,gemini", id="settings-provider-chain", classes="settings-row")
            yield Input(placeholder="OpenAI API key", password=True, id="settings-openai-api-key", classes="settings-row")
            yield Input(
                placeholder="OpenAI key pool (comma-separated)",
                password=True,
                id="settings-openai-api-keys",
                classes="settings-row",
            )
            yield Input(placeholder="Gemini API key", password=True, id="settings-gemini-api-key", classes="settings-row")
            yield Input(placeholder="Codex API key", password=True, id="settings-codex-api-key", classes="settings-row")
            yield Input(
                placeholder="Anthropic API key",
                password=True,
                id="settings-anthropic-api-key",
                classes="settings-row",
            )
            yield Input(placeholder="Steel API key", password=True, id="settings-steel-api-key", classes="settings-row")
            yield Static("", id="settings-feedback")
        yield Footer()

    def on_mount(self) -> None:
        self._reload_from_store()

    def _stash_active_material(self) -> None:
        self._material_cache[self._active_material_key] = self.query_one("#settings-material-editor", TextArea).text

    def _load_active_material(self) -> None:
        self.query_one("#settings-material-editor", TextArea).text = self._material_cache.get(self._active_material_key, "")

    def _render_onboarding_summary(self, bootstrap: dict) -> str:
        onboarding = bootstrap.get("onboarding") or {}
        required = onboarding.get("required_materials") or {}
        recommended = onboarding.get("recommended_materials") or {}
        return "\n".join(
            [
                f"Onboarding complete: {'yes' if onboarding.get('complete') else 'no'}",
                f"Master resume ready: {'yes' if required.get('master_resume') else 'no'}",
                f"Provider credentials ready: {'yes' if onboarding.get('credentials_ready') else 'no'}",
                f"Work stories ready: {'yes' if recommended.get('work_stories') else 'no'}",
                f"Candidate context ready: {'yes' if recommended.get('candidate_context') else 'no'}",
                f"Application profile ready: {'yes' if recommended.get('application_profile') else 'no'}",
            ]
        )

    def _apply_store_payload(self, settings_payload: dict, bootstrap_payload: dict) -> None:
        materials = settings_payload.get("materials") or {}
        self._material_meta = {key: dict(value) for key, value in materials.items()}
        for _, key in _SETTINGS_MATERIAL_KEYS:
            self._material_cache[key] = str((materials.get(key) or {}).get("content") or "")
        self._load_active_material()

        providers = settings_payload.get("providers") or {}
        default_provider = providers.get("default_provider") or "openai"
        self.query_one("#settings-default-provider", Select).value = default_provider
        self.query_one("#settings-provider-chain", Input).value = str(providers.get("provider_chain") or "")

        for _, input_id in _SETTINGS_CREDENTIAL_FIELDS:
            self.query_one(f"#{input_id}", Input).value = ""

        self.query_one("#settings-onboarding", Static).update(self._render_onboarding_summary(bootstrap_payload))
        self.query_one("#settings-feedback", Static).update("")

    def _reload_from_store(self) -> None:
        self._apply_store_payload(load_settings(), load_bootstrap())

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "settings-material-select":
            return
        self._stash_active_material()
        if isinstance(event.value, str) and event.value:
            self._active_material_key = event.value
        self._load_active_material()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-settings-save":
            self._save_current_settings()
        elif event.button.id == "btn-settings-import":
            self._import_current_material()

    def _save_current_settings(self) -> None:
        self._stash_active_material()
        materials_payload = {
            key: value
            for key, value in self._material_cache.items()
            if str(value).strip() or bool((self._material_meta.get(key) or {}).get("exists"))
        }
        payload = {
            "materials": materials_payload,
            "providers": {
                "default_provider": self.query_one("#settings-default-provider", Select).value,
                "provider_chain": self.query_one("#settings-provider-chain", Input).value.strip(),
            },
            "credentials": {
                key: self.query_one(f"#{input_id}", Input).value.strip()
                for key, input_id in _SETTINGS_CREDENTIAL_FIELDS
                if self.query_one(f"#{input_id}", Input).value.strip()
            },
        }
        settings_payload = save_settings(payload)
        self._apply_store_payload(settings_payload, load_bootstrap())
        self.query_one("#settings-feedback", Static).update("[green]Settings saved[/]")

    def _import_current_material(self) -> None:
        source = self.query_one("#settings-import-source", Input).value.strip()
        if not source:
            self.query_one("#settings-feedback", Static).update("[yellow]Enter a local path or public URL[/]")
            return

        try:
            if source.startswith(("http://", "https://")):
                result = import_material(self._active_material_key, source_url=source)
            else:
                path = Path(source).expanduser()
                result = import_material(
                    self._active_material_key,
                    file_name=path.name,
                    content_bytes=path.read_bytes(),
                )
        except Exception as exc:  # noqa: BLE001
            self.query_one("#settings-feedback", Static).update(f"[red]{escape(str(exc))}[/]")
            return

        self._apply_store_payload(result["settings"], result["bootstrap"])
        self.query_one("#settings-import-source", Input).value = ""
        label = _SETTINGS_MATERIAL_LABELS.get(self._active_material_key, self._active_material_key)
        self.query_one("#settings-feedback", Static).update(f"[green]{label} imported[/]")

    def action_switch_dash(self) -> None:
        self.app.switch_mode("dashboard")

    def action_switch_queue(self) -> None:
        self.app.switch_mode("queue")

    def action_switch_add(self) -> None:
        self.app.switch_mode("add")

    def action_switch_stats(self) -> None:
        self.app.switch_mode("stats")


# =========================================================================
# Main Application
# =========================================================================


class JobApp(App):
    TITLE = "Job Applications"
    theme = "solarized-light"
    CSS = """
    Screen {
        background: $surface;
    }
    DataTable:focus {
        border: solid $accent;
    }
    Input:focus {
        border: tall $accent;
    }
    TextArea:focus {
        border: tall $accent;
    }
    """

    MODES = {
        "dashboard": DashboardScreen,
        "queue": QueueScreen,
        "add": AddJobsScreen,
        "settings": SettingsScreen,
        "stats": StatsScreen,
    }

    BINDINGS = [
        Binding("d", "switch_mode('dashboard')", "Dashboard"),
        Binding("q", "switch_mode('queue')", "Queue"),
        Binding("a", "switch_mode('add')", "Add Jobs"),
        Binding("e", "switch_mode('settings')", "Settings"),
        Binding("s", "switch_mode('stats')", "Stats"),
        Binding("w", "toggle_workers", "Workers"),
        Binding("ctrl+r", "restart", "Restart"),
        Binding("question_mark", "show_help", "Help"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    _worker_proc: subprocess.Popen | None = None

    def on_mount(self) -> None:
        # Run schema + migrations once at TUI startup
        conn = init_db(DB_PATH)
        conn.close()
        self._start_workers()
        self.switch_mode("dashboard")

    def _is_worker_running(self) -> bool:
        """Check if the worker subprocess is alive."""
        if self._worker_proc and self._worker_proc.poll() is None:
            return True
        # Also check PID file in case worker was started externally
        worker_pid_file = _worker_pid_file()
        if worker_pid_file.exists():
            try:
                pid = int(worker_pid_file.read_text().strip())
                os.kill(pid, 0)  # signal 0 = check if alive
                return True
            except (ValueError, OSError):
                pass
        return False

    def _start_workers(self) -> None:
        """Start workers as a separate subprocess."""
        if self._is_worker_running():
            return
        ensure_action_allowed(
            "worker_control",
            metadata={"surface": "tui", "operation": "start"},
            environ=os.environ,
        )
        cmd = python_script_command(REPO_ROOT / "scripts" / "job_worker.py", "--workers", "20", "--headless")
        self._worker_proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group for clean kill
        )
        emit_trace(
            "worker_control",
            action="worker_control",
            metadata={"surface": "tui", "operation": "start", "workers": 20},
            environ=os.environ,
        )
        self.notify("Workers started (subprocess)")

    def _stop_workers(self) -> None:
        """Stop the worker subprocess and all its children (apply.sh, etc.)."""
        ensure_action_allowed(
            "worker_control",
            metadata={"surface": "tui", "operation": "stop"},
            environ=os.environ,
        )
        if self._worker_proc and self._worker_proc.poll() is None:
            # Kill entire process group to include child subprocesses
            try:
                os.killpg(os.getpgid(self._worker_proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                self._worker_proc.terminate()
            try:
                self._worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._worker_proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    self._worker_proc.kill()
            self._worker_proc = None
        elif _worker_pid_file().exists():
            try:
                pid = int(_worker_pid_file().read_text().strip())
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ValueError, OSError, ProcessLookupError):
                pass
        emit_trace(
            "worker_control",
            action="worker_control",
            metadata={"surface": "tui", "operation": "stop"},
            environ=os.environ,
        )
        # Reset all in-progress jobs back to queued
        conn = _get_conn()
        try:
            from job_db import reset_stale_jobs

            reset_stale_jobs(conn, stale_threshold_seconds=0)
            # Reset actively-processing jobs so they retry on restart
            conn.execute(
                "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, error_message = '', progress = '' "
                "WHERE status IN ('resolving', 'generating', 'autofilling', 'fix_in_progress', 'retrying')",
                (RETRY_AFTER_SENTINEL,),
            )
            conn.commit()
        finally:
            conn.close()
        self.notify("Workers stopped, all jobs reset to queued")

    def action_toggle_workers(self) -> None:
        """Toggle worker subprocess on/off."""
        if self._is_worker_running():
            self._stop_workers()
        else:
            self._start_workers()

    _RESTART_CODE = "__restart__"

    def action_restart(self) -> None:
        """Clean restart: kill all workers/pipelines, reset stuck jobs, relaunch TUI (Ctrl+R)."""
        self._stop_workers()
        # Also kill any orphaned processes not tracked by the TUI
        for pattern in ("job_worker", "apply.sh"):
            subprocess.run(["pkill", "-f", pattern], capture_output=True)
        self.exit(return_code=0, result=self._RESTART_CODE)

    def action_quit(self) -> None:
        self._stop_workers()
        self.exit()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())


def main() -> None:
    configure_runtime_trace(environ=os.environ, replace=True)
    while True:
        app = JobApp()
        result = app.run()
        if result != JobApp._RESTART_CODE:
            break


if __name__ == "__main__":
    main()
