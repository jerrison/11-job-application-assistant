"""FastAPI app for draft review — local server + optional tunnel."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from fastapi import Request as FastAPIRequest
except ImportError:  # pragma: no cover - optional web dependency
    FastAPIRequest = Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

log = logging.getLogger(__name__)


def create_app():
    """Create and return a FastAPI application for draft review.

    Importable so ``bin/job-assets draft serve`` can call it directly.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    app = FastAPI(title="Job Application Draft Review")

    def _open_db():
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from job_db import open_db_tracked

        return open_db_tracked(PROJECT_ROOT / "jobs.db", check_same_thread=False)

    def _request_action_audit(request: FastAPIRequest) -> tuple[dict | None, str | None]:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from job_action_audit import build_action_process_info, extract_action_detail_json_from_headers

        detail_json = extract_action_detail_json_from_headers(request.headers, route=request.url.path)
        return detail_json, build_action_process_info(detail_json)

    @app.exception_handler(sqlite3.DatabaseError)
    async def db_error_handler(request: FastAPIRequest, exc: sqlite3.DatabaseError) -> JSONResponse:
        log.error("Database error on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"detail": "Database temporarily unavailable. Please retry."},
        )

    # ── API endpoints ────────────────────────────────────────────────────────

    @app.get("/api/drafts")
    def list_drafts():
        conn = _open_db()
        try:
            rows = conn.execute(
                "SELECT id, company, role_title, board, output_dir, updated_at "
                "FROM jobs WHERE status = 'draft' ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @app.get("/api/drafts/{job_id}")
    def get_draft(job_id: int):
        conn = _open_db()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND status = 'draft'",
                (job_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Draft not found")
            job = dict(row)
            out_dir = Path(job["output_dir"]) if job.get("output_dir") else None
            if out_dir:
                summary_path = out_dir / "draft_summary.md"
                job["summary_md"] = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
            else:
                job["summary_md"] = ""
            return job
        finally:
            conn.close()

    @app.get("/api/drafts/{job_id}/images/{image_type}")
    def get_image(job_id: int, image_type: str):
        from application_submit_common import resolve_current_submit_artifacts
        from output_layout import role_submit_dir

        conn = _open_db()
        try:
            row = conn.execute(
                "SELECT output_dir, board FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            out_dir = Path(row["output_dir"]) if row["output_dir"] else None
            if not out_dir:
                raise HTTPException(404, "No output directory")
            submit_dir = role_submit_dir(out_dir)
            if image_type == "summary":
                path = out_dir / "draft_summary.png"
            elif image_type == "pre-submit":
                proof = resolve_current_submit_artifacts(
                    out_dir, board_name=row["board"], submit_dirname=submit_dir.name
                )
                path = proof.get("pre_submit_screenshot") or next(submit_dir.glob("*_pre_submit.png"), None)
            else:
                raise HTTPException(400, "Invalid image type — use 'summary' or 'pre-submit'")
            if not path or not path.exists():
                raise HTTPException(404, "Image not found")
            return FileResponse(str(path), media_type="image/png")
        finally:
            conn.close()

    @app.post("/api/drafts/{job_id}/approve")
    def approve_draft(job_id: int, request: FastAPIRequest):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from pipeline_orchestrator import approve_job, approve_job_failure_message

        conn = _open_db()
        try:
            action_detail_json, action_process_info = _request_action_audit(request)
            if not approve_job(
                conn,
                job_id,
                initiator="draft_web",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            ):
                detail = approve_job_failure_message(conn, job_id)
                status_code = 409 if "incomplete draft" in detail.casefold() else 400
                raise HTTPException(status_code, detail)
        finally:
            conn.close()
        return {"status": "approved", "job_id": job_id}

    @app.post("/api/drafts/{job_id}/reject")
    def reject_draft(job_id: int, request: FastAPIRequest):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from job_db import update_status

        conn = _open_db()
        try:
            action_detail_json, action_process_info = _request_action_audit(request)
            update_status(
                conn,
                job_id,
                "stopped",
                initiator="draft_web",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
        finally:
            conn.close()
        return {"status": "rejected", "job_id": job_id}

    @app.post("/api/drafts/{job_id}/regenerate")
    def regenerate_draft(job_id: int, request: FastAPIRequest):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from pipeline_orchestrator import regenerate_job

        conn = _open_db()
        try:
            action_detail_json, action_process_info = _request_action_audit(request)
            if not regenerate_job(
                conn,
                job_id,
                initiator="draft_web",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            ):
                raise HTTPException(409, "Job is not in draft status")
        finally:
            conn.close()
        return {"status": "regenerating", "job_id": job_id}

    @app.post("/api/drafts/{job_id}/reset")
    def reset_draft(job_id: int, request: FastAPIRequest):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from pipeline_orchestrator import reset_job_to_new

        conn = _open_db()
        try:
            action_detail_json, action_process_info = _request_action_audit(request)
            if not reset_job_to_new(
                conn,
                job_id,
                initiator="draft_web",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            ):
                raise HTTPException(409, "Job cannot be reset to new")
        finally:
            conn.close()
        return {"status": "queued", "job_id": job_id}

    @app.post("/api/drafts/{job_id}/mark-reviewed")
    def mark_reviewed(job_id: int, request: FastAPIRequest):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from sweep_controller import record_transition

        conn = _open_db()
        try:
            row = conn.execute(
                "SELECT id FROM jobs WHERE id = ? AND status = 'draft'",
                (job_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Draft not found")
            action_detail_json, _action_process_info = _request_action_audit(request)
        finally:
            conn.close()

        manifest_path = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
        recorded = record_transition(
            manifest_path=manifest_path,
            phase_key="phase3",
            row_id=str(job_id),
            outcome="reviewed_ready",
            handled_via="draft_web_browser",
            notes="Recorded from draft_web browser review.",
            detail_json=action_detail_json,
        )
        return {
            "status": "recorded",
            "job_id": job_id,
            "outcome": recorded["outcome"],
            "review_trace_path": recorded["review_trace_path"],
            "artifact_manifest_path": recorded["artifact_manifest_path"],
            "linear_sync_status": recorded.get("linear_sync_status", ""),
        }

    @app.put("/api/drafts/{job_id}/overrides")
    def update_overrides(job_id: int, overrides: dict):
        conn = _open_db()
        try:
            row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            out_dir = Path(row["output_dir"]) if row["output_dir"] else None
            if not out_dir:
                raise HTTPException(404, "No output directory")
        finally:
            conn.close()

        overrides_path = out_dir / "draft_overrides.json"
        existing: dict = {}
        if overrides_path.exists():
            try:
                existing = json.loads(overrides_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        existing.update(overrides)
        overrides_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return {"status": "updated", "overrides": existing}

    # ── HTML frontend (inline) ───────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        drafts = list_drafts()
        rows_html = "".join(
            f"<tr>"
            f'<td><a href="/drafts/{d["id"]}">{d["id"]}</a></td>'
            f"<td>{d.get('company') or '?'}</td>"
            f"<td>{d.get('role_title') or '?'}</td>"
            f"<td>{d.get('board') or '?'}</td>"
            f"<td>{d.get('updated_at') or '?'}</td>"
            f"</tr>"
            for d in drafts
        )
        return (
            "<!DOCTYPE html><html><head><title>Draft Review</title>"
            "<style>"
            "body{font-family:system-ui;max-width:1200px;margin:0 auto;padding:2rem}"
            "table{width:100%;border-collapse:collapse}"
            "th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left}"
            "a{color:#2563eb;text-decoration:none}"
            "a:hover{text-decoration:underline}"
            "</style>"
            "</head><body>"
            "<h1>Application Drafts</h1>"
            "<table>"
            "<tr><th>ID</th><th>Company</th><th>Role</th><th>Board</th><th>Updated</th></tr>"
            f"{rows_html}"
            "</table>"
            "</body></html>"
        )

    @app.get("/drafts/{job_id}", response_class=HTMLResponse)
    def draft_detail_page(job_id: int):
        import markdown as md_lib

        draft = get_draft(job_id)
        summary_html = md_lib.markdown(draft.get("summary_md", ""))
        company = draft.get("company") or "?"
        role = draft.get("role_title") or "?"
        return (
            f"<!DOCTYPE html><html><head><title>Draft #{job_id}</title>"
            "<style>"
            "body{font-family:system-ui;max-width:1400px;margin:0 auto;padding:2rem}"
            ".layout{display:grid;grid-template-columns:1fr 1fr;gap:2rem}"
            ".actions{margin:1rem 0}"
            "button{padding:8px 16px;margin-right:8px;cursor:pointer;border:1px solid #ccc;"
            "border-radius:4px;background:#f9fafb;font-size:14px}"
            "button:hover{background:#e5e7eb}"
            "button.approve{background:#16a34a;color:white;border-color:#16a34a}"
            "button.approve:hover{background:#15803d}"
            "button.reject{background:#dc2626;color:white;border-color:#dc2626}"
            "button.reject:hover{background:#b91c1c}"
            "img{max-width:100%;border:1px solid #ddd;border-radius:4px}"
            "h3{margin-top:1.5rem}"
            "</style>"
            "</head><body>"
            f"<h1>{company} &mdash; {role}</h1>"
            '<div class="actions">'
            f'<button class="approve" onclick="fetch(\'/api/drafts/{job_id}/approve\','
            "{method:'POST'}).then(()=>location.reload())\">Approve</button>"
            f'<button class="reject" onclick="fetch(\'/api/drafts/{job_id}/reject\','
            "{method:'POST'}).then(()=>location.reload())\">Reject</button>"
            f"<button onclick=\"fetch('/api/drafts/{job_id}/regenerate',"
            "{method:'POST'}).then(()=>location.reload())\">Regenerate</button>"
            f"<button onclick=\"fetch('/api/drafts/{job_id}/reset',"
            "{method:'POST'}).then(()=>location.reload())\">Reset to New</button>"
            f"<button onclick=\"fetch('/api/drafts/{job_id}/mark-reviewed',"
            "{method:'POST'}).then(()=>location.reload())\">Mark Reviewed</button>"
            "</div>"
            '<div class="layout">'
            f"<div>{summary_html}</div>"
            "<div>"
            "<h3>Pre-Submit Screenshot</h3>"
            f'<img src="/api/drafts/{job_id}/images/pre-submit" '
            "onerror=\"this.outerHTML='<p>No screenshot available</p>'\">"
            "<h3>Draft Summary</h3>"
            f'<img src="/api/drafts/{job_id}/images/summary" '
            "onerror=\"this.outerHTML='<p>No summary image available</p>'\">"
            "</div>"
            "</div>"
            "</body></html>"
        )

    return app


def serve(host: str = "127.0.0.1", port: int = 8420) -> None:
    """Start the uvicorn server — called by ``bin/job-assets draft serve``."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    serve()
