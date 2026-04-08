# Interview Prep Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add interview preparation guide generation — a Claude CLI subprocess that does deep web research and produces a personalized 9-section prep guide as markdown, .docx, and .pdf.

**Architecture:** New script `generate_interview_prep.py` spawns `claude --print` with a system prompt + embedded context. Output lands in `output/{company}/{role}/interview_prep/`. Web UI adds an "Interview Prep" tab with generate modal. API endpoint spawns generation in a background thread.

**Tech Stack:** Python 3.14, Claude CLI (`claude --print`), python-docx, LibreOffice (pdf), FastAPI, vanilla JS SPA

**Spec:** `docs/superpowers/specs/2026-03-20-interview-prep-guide-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `scripts/generate_interview_prep.py` | CLI entry point: gathers context, spawns Claude CLI, builds docx/pdf |
| `scripts/prompts/interview_prep_system.md` | System prompt for the Claude CLI subprocess |

### Modified Files
| File | Change |
|------|--------|
| `scripts/job_web.py` | 3 new endpoints: POST generate, GET status/content, GET download |
| `scripts/static/index.html` | New tab button + tab content div + interview prep modal HTML |
| `scripts/static/app.js` | Tab logic, modal open/close, generate API call, polling, markdown rendering |
| `scripts/static/style.css` | Styles for prep tab content + markdown rendering |

---

## Task 1: Create the system prompt

**Files:**
- Create: `scripts/prompts/interview_prep_system.md`

- [ ] **Step 1: Create prompts directory**

```bash
mkdir -p scripts/prompts
```

- [ ] **Step 2: Write the system prompt file**

Create `scripts/prompts/interview_prep_system.md` with the full interview prep system prompt. This is the core intelligence — it tells Claude how to research, what sections to produce, how to calibrate by level/company-type, and how to map the user's stories.

Key sections to include:
- Role definition (interview prep strategist)
- Level calibration table (FAANG L6 vs. startup stage mapping: Series A-B / B-C / D+)
- Context file usage instructions (how to read and index work_stories.md, master_resume.md, candidate_context.md)
- Story indexing protocol (build internal index, tag competencies, flag gaps)
- Company type detection (classify from JD + research)
- Research protocol (web search targets: company deep dive, interview format from Glassdoor/Blind/coaching sites, interviewer profiles)
- 9-section output structure with per-section guidance:
  1. Executive Summary
  2. Company Intelligence
  3. Interview Format & Process
  4. Interviewer Profiles (conditional)
  5. Behavioral Questions (12-15)
  6. Product Sense Questions (8-10)
  7. Execution & Technical Questions (8-12)
  8. Questions to Ask (10-12)
  9. Preparation Strategy
- Quality filters (level calibration > company-type > personalization)
- Output format instructions (write to the specified path as markdown)

The prompt should be self-contained — Claude CLI gets no other system instructions.

- [ ] **Step 3: Commit**

```bash
git add scripts/prompts/interview_prep_system.md
git commit -m "feat: add interview prep system prompt"
```

---

## Task 2: Core generation script — context gathering and Claude CLI spawn

**Files:**
- Create: `scripts/generate_interview_prep.py`

- [ ] **Step 1: Write the script skeleton with argparse**

```python
#!/usr/bin/env python3
"""Generate an interview preparation guide for a job application."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Generate interview prep guide")
    parser.add_argument("output_dir", help="Job output directory (e.g., output/uber/senior-pm-web)")
    parser.add_argument("--stage", default="General",
                        choices=["General", "Recruiter Screen", "Phone Screen", "Onsite", "Final Round"])
    parser.add_argument("--interviewer", action="append", default=[], dest="interviewers",
                        help="Interviewer info (repeatable): 'Name, Title, linkedin.com/in/...'")
    parser.add_argument("--notes", default="", help="Additional notes or focus areas")
    parser.add_argument("--force", action="store_true", help="Regenerate even if prep already exists")
    args = parser.parse_args()

    out_dir = Path(args.output_dir).resolve()
    if not out_dir.exists():
        print(f"Error: output directory does not exist: {out_dir}", file=sys.stderr)
        sys.exit(1)

    prep_dir = out_dir / "interview_prep"
    prep_md = prep_dir / "interview_prep.md"

    if prep_md.exists() and not args.force:
        print(f"Interview prep already exists: {prep_md}")
        print("Use --force to regenerate.")
        sys.exit(0)

    prep_dir.mkdir(parents=True, exist_ok=True)
    generate_prep(out_dir, prep_dir, stage=args.stage,
                  interviewers=args.interviewers, notes=args.notes)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Implement `generate_prep()` — context gathering**

Add function that reads all context files and builds the prompt:

```python
def _read_file(path: Path) -> str:
    """Read a file, return empty string if missing."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except FileNotFoundError:
        return ""


def _write_progress(prep_dir: Path, status: str, detail: str):
    """Write progress file for web UI polling."""
    (prep_dir / ".progress.json").write_text(
        json.dumps({"status": status, "detail": detail,
                     "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def generate_prep(
    out_dir: Path,
    prep_dir: Path,
    *,
    stage: str = "General",
    interviewers: list[str] | None = None,
    notes: str = "",
):
    """Run the full interview prep generation pipeline."""
    _write_progress(prep_dir, "starting", "Gathering context...")

    # Read pipeline meta
    meta_path = out_dir / ".pipeline_meta.json"
    if not meta_path.exists():
        print(f"Error: no .pipeline_meta.json in {out_dir}", file=sys.stderr)
        sys.exit(1)
    meta = json.loads(meta_path.read_text())
    company = meta.get("company_proper") or meta.get("company", "Unknown")
    jd_title = meta.get("jd_title", "Unknown Role")
    jd_source = meta.get("jd_source", "")

    # Gather context files
    content_dir = out_dir / "content"
    context_parts = []

    jd_raw = _read_file(content_dir / "jd_raw.md")
    if jd_raw:
        context_parts.append(f"## Job Description (Raw)\n\n{jd_raw}")

    jd_parsed = _read_file(content_dir / "jd_parsed.json")
    if jd_parsed:
        context_parts.append(f"## Job Description (Parsed JSON)\n\n{jd_parsed}")

    research_cache = _read_file(content_dir / "role_research_cache.json")
    if research_cache:
        context_parts.append(f"## Existing Research Cache\n\n{research_cache}")

    master_resume = _read_file(PROJECT_ROOT / "master_resume.md")
    if master_resume:
        context_parts.append(f"## Master Resume\n\n{master_resume}")

    work_stories = _read_file(PROJECT_ROOT / "work_stories.md")
    if work_stories:
        context_parts.append(f"## Work Stories (STAR Narratives)\n\n{work_stories}")

    candidate_ctx = _read_file(PROJECT_ROOT / "candidate_context.md")
    if candidate_ctx:
        context_parts.append(f"## Candidate Context\n\n{candidate_ctx}")

    app_profile = _read_file(PROJECT_ROOT / "application_profile.md")
    if app_profile:
        context_parts.append(f"## Application Profile\n\n{app_profile}")

    # Build user message
    interviewer_text = "\n".join(f"- {i}" for i in (interviewers or [])) or "Not provided"
    prep_md_path = prep_dir / "interview_prep.md"

    user_message = f"""Generate an interview preparation guide for:
- Company: {company}
- Role: {jd_title}
- JD URL: {jd_source}
- Interview Stage: {stage}
- Interviewers:
{interviewer_text}
- Additional Notes: {notes or "None"}

Write the complete guide to: {prep_md_path}

---

# Context Files

{chr(10).join(context_parts)}
"""

    # Spawn Claude CLI
    _run_claude(user_message, prep_dir, prep_md_path)

    # Post-process: build docx and pdf
    if prep_md_path.exists():
        _write_progress(prep_dir, "building_documents", "Building .docx and .pdf...")
        _build_docx_and_pdf(prep_md_path, company)
        _write_progress(prep_dir, "complete", "Interview prep guide ready.")
        print(f"\nInterview prep guide generated:")
        print(f"  Markdown: {prep_md_path}")
        docx_path = next(prep_dir.glob("*.docx"), None)
        pdf_path = next(prep_dir.glob("*.pdf"), None)
        if docx_path:
            print(f"  Word:     {docx_path}")
        if pdf_path:
            print(f"  PDF:      {pdf_path}")
    else:
        _write_progress(prep_dir, "failed", "Claude CLI did not produce interview_prep.md")
        print("Error: Claude CLI did not produce interview_prep.md", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 3: Implement `_run_claude()` — Claude CLI subprocess**

```python
def _run_claude(user_message: str, prep_dir: Path, output_path: Path):
    """Spawn claude CLI to generate the interview prep guide."""
    if not shutil.which("claude"):
        print("Error: claude CLI not found on PATH", file=sys.stderr)
        sys.exit(1)

    _write_progress(prep_dir, "researching", "Claude is researching and generating the prep guide...")

    system_prompt_path = Path(__file__).parent / "prompts" / "interview_prep_system.md"
    if not system_prompt_path.exists():
        print(f"Error: system prompt not found: {system_prompt_path}", file=sys.stderr)
        sys.exit(1)

    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    # Build the full prompt with system instructions embedded
    full_prompt = f"""<system>
{system_prompt}
</system>

{user_message}"""

    result = subprocess.run(
        ["claude", "--print", "-p", full_prompt,
         "--allowedTools", "WebSearch,WebFetch,Read,Write,Glob,Grep,mcp__plugin_playwright_playwright__browser_navigate,mcp__plugin_playwright_playwright__browser_snapshot,mcp__plugin_playwright_playwright__browser_click,mcp__plugin_playwright_playwright__browser_take_screenshot"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=900,  # 15 minutes max
    )

    if result.returncode != 0:
        _write_progress(prep_dir, "failed", f"Claude CLI exited with code {result.returncode}")
        print(f"Claude CLI stderr:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # If Claude didn't write the file via Write tool, the output is in stdout
    if not output_path.exists() and result.stdout.strip():
        output_path.write_text(result.stdout.strip(), encoding="utf-8")
```

- [ ] **Step 4: Commit**

```bash
git add scripts/generate_interview_prep.py
git commit -m "feat: interview prep generation script — context gathering + Claude CLI spawn"
```

---

## Task 3: Document generation — markdown to .docx and .pdf

**Files:**
- Modify: `scripts/generate_interview_prep.py`

- [ ] **Step 1: Implement `_build_docx_and_pdf()`**

Add to `generate_interview_prep.py`. Parses the markdown and renders a formatted Word document, then converts to PDF via LibreOffice. Follow the patterns from `build_cover_letter.py` (Calibri font, US Letter, `_run()` helper for styled runs, LibreOffice with temp profile).

```python
import re
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor


FONT = "Calibri"
COLOR_BLACK = RGBColor(0x33, 0x33, 0x33)
COLOR_BLUE = RGBColor(0x2B, 0x57, 0x9A)
COLOR_GRAY = RGBColor(0x55, 0x55, 0x55)
COLOR_RED = RGBColor(0xCC, 0x33, 0x33)
CANDIDATE_NAME = "Jerrison Li"


def _docx_run(para, text, size, bold=False, italic=False, color=COLOR_BLACK):
    """Add a styled run to a paragraph (matches build_cover_letter.py pattern)."""
    run = para.add_run(text)
    run.font.name = FONT
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} w:ascii="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/>')
        rPr.insert(0, rFonts)
    return run


def _build_docx_and_pdf(md_path: Path, company: str):
    """Convert interview_prep.md to .docx and .pdf."""
    text = md_path.read_text(encoding="utf-8")
    prep_dir = md_path.parent
    docx_name = f"{CANDIDATE_NAME} Interview Prep - {company}.docx"
    docx_path = prep_dir / docx_name

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(0.75)
    section.right_margin = Inches(0.75)

    # Default style
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(11)
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(4)
    style.paragraph_format.line_spacing = 1.15

    # Parse markdown line by line and render
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Heading levels
        if stripped.startswith("# ") and not stripped.startswith("## "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(18)
            p.paragraph_format.space_after = Pt(8)
            _docx_run(p, stripped[2:], Pt(18), bold=True, color=COLOR_BLUE)
        elif stripped.startswith("## "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(14)
            p.paragraph_format.space_after = Pt(6)
            _docx_run(p, stripped[3:], Pt(14), bold=True, color=COLOR_BLUE)
        elif stripped.startswith("### "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(4)
            _docx_run(p, stripped[4:], Pt(12), bold=True, color=COLOR_BLACK)
        elif stripped.startswith("#### "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(2)
            _docx_run(p, stripped[5:], Pt(11), bold=True, italic=True, color=COLOR_BLACK)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            # Bullet point
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.25)
            p.paragraph_format.space_after = Pt(2)
            bullet_text = stripped[2:]
            # Handle bold prefix (e.g., "- **Key point**: detail")
            bold_match = re.match(r"\*\*(.+?)\*\*:?\s*(.*)", bullet_text)
            if bold_match:
                _docx_run(p, bold_match.group(1), Pt(11), bold=True)
                if bold_match.group(2):
                    _docx_run(p, ": " + bold_match.group(2) if bullet_text.count(":") else " " + bold_match.group(2), Pt(11))
            else:
                _docx_run(p, bullet_text, Pt(11))
        elif stripped.startswith("---"):
            # Horizontal rule — skip (used as section separators in md)
            pass
        elif stripped == "":
            # Blank line — small spacer
            pass
        elif stripped.startswith(">"):
            # Blockquote
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.space_after = Pt(4)
            _docx_run(p, stripped.lstrip("> "), Pt(11), italic=True, color=COLOR_GRAY)
        elif stripped.startswith("```"):
            # Code block — collect until closing ```
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if code_lines:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.2)
                p.paragraph_format.space_after = Pt(4)
                run = p.add_run("\n".join(code_lines))
                run.font.name = "Courier New"
                run.font.size = Pt(9)
                run.font.color.rgb = COLOR_GRAY
        else:
            # Regular paragraph — handle inline bold/italic
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            _render_inline_markdown(p, stripped)

        i += 1

    doc.save(str(docx_path))
    print(f"  .docx saved: {docx_path}")

    # Convert to PDF
    _convert_to_pdf(docx_path)


def _render_inline_markdown(para, text: str):
    """Render inline **bold** and *italic* markdown as docx runs."""
    # Split on bold (**...**) and italic (*...*)
    parts = re.split(r"(\*\*.*?\*\*|\*.*?\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            _docx_run(para, part[2:-2], Pt(11), bold=True)
        elif part.startswith("*") and part.endswith("*"):
            _docx_run(para, part[1:-1], Pt(11), italic=True)
        else:
            _docx_run(para, part, Pt(11))


def _find_libreoffice() -> str | None:
    """Find LibreOffice binary."""
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        shutil.which("soffice"),
        shutil.which("libreoffice"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _convert_to_pdf(docx_path: Path):
    """Convert .docx to .pdf via LibreOffice."""
    soffice = _find_libreoffice()
    if not soffice:
        print("  Warning: LibreOffice not found — skipping PDF conversion.")
        return

    out_dir = str(docx_path.parent)
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"

    with tempfile.TemporaryDirectory(prefix="libreoffice-profile-") as profile_dir:
        result = subprocess.run(
            [soffice,
             f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
             "--headless", "--nologo", "--nodefault",
             "--nolockcheck", "--nofirststartwizard",
             "--convert-to", "pdf",
             "--outdir", out_dir,
             str(docx_path)],
            env=env, capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            pdf_name = docx_path.stem + ".pdf"
            print(f"  .pdf saved: {docx_path.parent / pdf_name}")
        else:
            print(f"  Warning: PDF conversion failed: {result.stderr[:200]}")
```

- [ ] **Step 2: Test CLI end-to-end manually**

```bash
# Test with a job that has full pipeline output
uv run python scripts/generate_interview_prep.py output/uber/senior-pm-web --stage "Phone Screen" --force
```

Verify:
- `output/uber/senior-pm-web/interview_prep/interview_prep.md` exists and has all 9 sections
- `output/uber/senior-pm-web/interview_prep/Jerrison Li Interview Prep - Uber.docx` exists and is well-formatted
- `output/uber/senior-pm-web/interview_prep/Jerrison Li Interview Prep - Uber.pdf` exists
- `.progress.json` shows `"status": "complete"`

- [ ] **Step 3: Commit**

```bash
git add scripts/generate_interview_prep.py
git commit -m "feat: interview prep docx/pdf generation from markdown"
```

---

## Task 4: Web API endpoints

**Files:**
- Modify: `scripts/job_web.py`

- [ ] **Step 1: Add the 3 API endpoints**

Add these near the other job action endpoints (after the `/api/jobs/{job_id}/retry` route). Ensure these imports exist at the top of `job_web.py`:

```python
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
# Note: FileResponse is already imported via fastapi.responses — do NOT add a starlette duplicate
```

```python
# --- Interview Prep endpoints ---

@app.get("/api/jobs/{job_id}/interview-prep")
async def get_interview_prep(job_id: int):
    """Get interview prep status and content."""
    conn = get_conn()
    row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["output_dir"]:
        raise HTTPException(404, "Job not found or no output directory")

    prep_dir = Path(row["output_dir"]) / "interview_prep"
    prep_md = prep_dir / "interview_prep.md"
    progress_file = prep_dir / ".progress.json"
    generating_file = prep_dir / ".generating"

    # Check if currently generating
    is_generating = False
    if generating_file.exists():
        try:
            pid = int(generating_file.read_text().strip())
            # Check if PID is still alive
            os.kill(pid, 0)
            is_generating = True
        except (ValueError, ProcessLookupError, PermissionError):
            generating_file.unlink(missing_ok=True)

    if prep_md.exists():
        md_content = prep_md.read_text(encoding="utf-8")
        return {
            "exists": True,
            "generating": is_generating,
            "markdown": md_content,
            "docx_download": f"/api/jobs/{job_id}/interview-prep/download/docx",
            "pdf_download": f"/api/jobs/{job_id}/interview-prep/download/pdf",
        }

    # Check progress
    progress = None
    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text())
        except Exception:
            pass

    return {"exists": False, "generating": is_generating, "progress": progress}


@app.post("/api/jobs/{job_id}/interview-prep")
async def generate_interview_prep_endpoint(job_id: int, request: Request):
    """Start interview prep generation."""
    conn = get_conn()
    row = conn.execute("SELECT output_dir, company, role_title FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["output_dir"]:
        raise HTTPException(404, "Job not found or no output directory")

    prep_dir = Path(row["output_dir"]) / "interview_prep"
    generating_file = prep_dir / ".generating"

    # Concurrency guard
    if generating_file.exists():
        try:
            pid = int(generating_file.read_text().strip())
            os.kill(pid, 0)
            raise HTTPException(409, "Interview prep generation already in progress")
        except (ValueError, ProcessLookupError, PermissionError):
            generating_file.unlink(missing_ok=True)

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    stage = body.get("stage", "General")
    interviewers = body.get("interviewers", "")
    notes = body.get("notes", "")

    # Build CLI args
    cmd = ["uv", "run", "python", "scripts/generate_interview_prep.py",
           row["output_dir"], "--stage", stage, "--force"]
    if interviewers:
        for line in interviewers.strip().splitlines():
            line = line.strip()
            if line:
                cmd.extend(["--interviewer", line])
    if notes:
        cmd.extend(["--notes", notes])

    # Log event
    log_event(conn, job_id, "interview_prep_started",
              detail_json={"stage": stage, "interviewers": interviewers, "notes": notes},
              initiator="web")

    # Spawn in background thread
    def _run_prep():
        import sqlite3 as _sql
        bg_conn = _sql.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        bg_conn.row_factory = _sql.Row
        bg_conn.execute("PRAGMA journal_mode=WAL")
        prep_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            generating_file.write_text(str(proc.pid))
            stdout, stderr = proc.communicate(timeout=900)
            if proc.returncode == 0:
                log_event(bg_conn, job_id, "interview_prep_completed", initiator="web")
            else:
                log_event(bg_conn, job_id, "interview_prep_failed",
                          detail=stderr[:500], initiator="web")
        except subprocess.TimeoutExpired:
            log_event(bg_conn, job_id, "interview_prep_failed",
                      detail="Generation timed out after 15 minutes", initiator="web")
        except Exception as exc:
            log_event(bg_conn, job_id, "interview_prep_failed",
                      detail=str(exc)[:500], initiator="web")
        finally:
            generating_file.unlink(missing_ok=True)
            bg_conn.close()

    threading.Thread(target=_run_prep, daemon=True).start()
    return {"status": "started"}


@app.get("/api/jobs/{job_id}/interview-prep/download/{fmt}")
async def download_interview_prep(job_id: int, fmt: str):
    """Download interview prep document."""
    if fmt not in ("docx", "pdf"):
        raise HTTPException(400, "Format must be 'docx' or 'pdf'")

    conn = get_conn()
    row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["output_dir"]:
        raise HTTPException(404, "Job not found")

    prep_dir = Path(row["output_dir"]) / "interview_prep"
    matches = list(prep_dir.glob(f"*.{fmt}"))
    if not matches:
        raise HTTPException(404, f"No .{fmt} file found")

    file_path = matches[0]
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "application/pdf"
    return FileResponse(file_path, media_type=media_type, filename=file_path.name)
```

- [ ] **Step 2: Verify imports and existing patterns**

Check that `log_event`, `get_conn`, `DB_PATH`, `PROJECT_ROOT`, `threading`, `subprocess`, `json`, `os`, `Path`, and `FileResponse` are already available in `job_web.py` scope. `Request` was added to the fastapi import above. Add any other missing imports.

- [ ] **Step 3: Commit**

```bash
git add scripts/job_web.py
git commit -m "feat: interview prep API endpoints (POST generate, GET status, GET download)"
```

---

## Task 5: Web UI — tab, modal, and JavaScript

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/style.css`

- [ ] **Step 1: Add the Interview Prep tab button and content div to `index.html`**

In `index.html`, add the tab button after the Timeline button (line 187):

```html
<button class="tab-btn" data-tab="interview-prep" onclick="switchTab('interview-prep')">Interview Prep</button>
```

Add the tab content div after the timeline tab content div (after line 212):

```html
<!-- Interview Prep tab -->
<div class="tab-content" id="tab-interview-prep" style="display:none">
  <div id="interview-prep-content">
    <div class="loading-msg">Loading interview prep...</div>
  </div>
</div>
```

Add the interview prep modal HTML before the toast container (before line 575):

```html
<!-- Interview Prep modal -->
<div class="modal-backdrop" id="prep-backdrop" style="display:none" onclick="closePrepModal()"></div>
<div class="modal" id="prep-modal" style="display:none">
  <div class="modal-header">
    <h2>Generate Interview Prep</h2>
    <button class="modal-close" onclick="closePrepModal()">&times;</button>
  </div>
  <div class="modal-body">
    <label class="form-label">Interview Stage</label>
    <select id="prep-stage" class="form-input">
      <option value="General">General</option>
      <option value="Recruiter Screen">Recruiter Screen</option>
      <option value="Phone Screen">Phone Screen</option>
      <option value="Onsite">Onsite</option>
      <option value="Final Round">Final Round</option>
    </select>
    <label class="form-label" style="margin-top:12px">Interviewers <span style="color:var(--base0);font-weight:normal">(optional — one per line)</span></label>
    <textarea id="prep-interviewers" class="form-input" rows="3" placeholder="Jane Doe, VP Product, linkedin.com/in/janedoe&#10;John Smith, Eng Director"></textarea>
    <label class="form-label" style="margin-top:12px">Notes <span style="color:var(--base0);font-weight:normal">(optional)</span></label>
    <textarea id="prep-notes" class="form-input" rows="2" placeholder="Focus on AI experience, worried about system design round..."></textarea>
  </div>
  <div class="modal-footer">
    <button class="btn btn-outline" onclick="closePrepModal()">Cancel</button>
    <button class="btn btn-primary" id="prep-generate-btn" onclick="submitPrepModal()">Generate</button>
  </div>
</div>
```

- [ ] **Step 2: Add JavaScript for tab, modal, polling, and markdown rendering in `app.js`**

Add the tab to the `switchTab` function's tab map. Then add these functions:

```javascript
// ── Interview Prep tab ──────────────────────────────────────
function loadInterviewPrepTab(jobId) {
  const container = document.getElementById('interview-prep-content');
  container.innerHTML = '<div class="loading-msg">Loading...</div>';

  fetch(`/api/jobs/${jobId}/interview-prep`)
    .then(r => r.json())
    .then(data => {
      if (data.exists) {
        // Render markdown content with download links
        let html = '<div class="prep-toolbar">';
        html += `<a href="${data.docx_download}" class="btn btn-outline btn-sm" download>Download .docx</a> `;
        html += `<a href="${data.pdf_download}" class="btn btn-outline btn-sm" download>Download .pdf</a> `;
        html += '<button class="btn btn-outline btn-sm" onclick="openPrepModal()">Regenerate</button>';
        html += '</div>';
        html += '<div class="prep-markdown">' + renderMarkdown(data.markdown) + '</div>';
        container.innerHTML = html;
      } else if (data.generating) {
        // Show progress
        const detail = data.progress ? data.progress.detail : 'Generating...';
        container.innerHTML = `<div class="prep-generating"><div class="spinner-sm"></div> ${escapeHtml(detail)}</div>`;
        // Poll every 3 seconds
        setTimeout(() => {
          if (currentTab === 'interview-prep' && currentJobId == jobId) {
            loadInterviewPrepTab(jobId);
          }
        }, 3000);
      } else {
        // Not generated yet — show generate button
        container.innerHTML = `
          <div class="prep-empty">
            <p>No interview prep guide generated yet.</p>
            <button class="btn btn-primary" onclick="openPrepModal()">Generate Interview Prep</button>
          </div>`;
      }
    })
    .catch(err => {
      container.innerHTML = `<div class="error-msg">Failed to load: ${escapeHtml(err.message)}</div>`;
    });
}

function renderMarkdown(md) {
  // Simple markdown-to-HTML renderer for display
  let html = md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    // Headings
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // Bold and italic
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Code blocks
    .replace(/```[\s\S]*?```/g, m => '<pre><code>' + m.slice(3, -3).replace(/^\w+\n/, '') + '</code></pre>')
    // Inline code
    .replace(/`(.+?)`/g, '<code>$1</code>')
    // Blockquotes
    .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>')
    // Bullet lists
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    // Horizontal rules
    .replace(/^---$/gm, '<hr>')
    // Paragraphs (double newlines)
    .replace(/\n\n/g, '</p><p>')
    // Single newlines within paragraphs
    .replace(/\n/g, '<br>');

  // Wrap loose <li> in <ul>
  html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, '<ul>$1</ul>');
  return '<div class="md-body"><p>' + html + '</p></div>';
}

// ── Prep Modal ──────────────────────────────────────────────
function openPrepModal() {
  document.getElementById('prep-backdrop').style.display = 'block';
  document.getElementById('prep-modal').style.display = 'flex';
}

function closePrepModal() {
  document.getElementById('prep-backdrop').style.display = 'none';
  document.getElementById('prep-modal').style.display = 'none';
}

function submitPrepModal() {
  const stage = document.getElementById('prep-stage').value;
  const interviewers = document.getElementById('prep-interviewers').value;
  const notes = document.getElementById('prep-notes').value;
  const btn = document.getElementById('prep-generate-btn');

  btn.disabled = true;
  btn.textContent = 'Starting...';

  fetch(`/api/jobs/${currentJobId}/interview-prep`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stage, interviewers, notes}),
  })
    .then(r => {
      if (r.status === 409) throw new Error('Generation already in progress');
      if (!r.ok) throw new Error('Failed to start generation');
      return r.json();
    })
    .then(() => {
      closePrepModal();
      showToast('Interview prep generation started — this takes 5-15 minutes.');
      // Switch to the tab and start polling
      switchTab('interview-prep');
    })
    .catch(err => {
      showToast(err.message, 'error');
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = 'Generate';
    });
}
```

Also update the `switchTab` function to handle the new tab — add to the tab map and the loader dispatch:

```javascript
// In the tabMap object, add:
'interview-prep': 'tab-interview-prep'

// In the loader dispatch, add:
if (tabName === 'interview-prep') loadInterviewPrepTab(currentJobId);
```

- [ ] **Step 3: Add CSS for prep tab content in `style.css`**

```css
/* ── Interview Prep tab ──────────────────────────────────── */
.prep-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--base2);
}

.prep-empty {
  text-align: center;
  padding: 40px 20px;
  color: var(--base0);
}

.prep-empty p { margin-bottom: 16px; }

.prep-generating {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 32px 20px;
  color: var(--base01);
  font-size: 14px;
}

.spinner-sm {
  width: 18px; height: 18px;
  border: 2px solid var(--base2);
  border-top-color: var(--blue);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Markdown rendering styles */
.prep-markdown .md-body { line-height: 1.65; color: var(--base01); }
.prep-markdown .md-body h1 { font-size: 20px; font-weight: 700; color: var(--base02); margin: 24px 0 12px; padding-bottom: 6px; border-bottom: 2px solid var(--base2); }
.prep-markdown .md-body h2 { font-size: 17px; font-weight: 700; color: var(--blue); margin: 20px 0 10px; }
.prep-markdown .md-body h3 { font-size: 14px; font-weight: 700; color: var(--base01); margin: 16px 0 6px; }
.prep-markdown .md-body h4 { font-size: 13px; font-weight: 600; color: var(--base01); margin: 12px 0 4px; }
.prep-markdown .md-body p { margin: 6px 0; }
.prep-markdown .md-body ul { margin: 6px 0 6px 20px; }
.prep-markdown .md-body li { margin: 3px 0; }
.prep-markdown .md-body strong { font-weight: 700; color: var(--base02); }
.prep-markdown .md-body code { background: var(--base2); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.prep-markdown .md-body pre { background: var(--base02); color: var(--base2); padding: 12px; border-radius: var(--radius); overflow-x: auto; margin: 8px 0; }
.prep-markdown .md-body pre code { background: none; padding: 0; color: inherit; }
.prep-markdown .md-body blockquote { border-left: 3px solid var(--blue); padding: 6px 12px; margin: 8px 0; color: var(--base0); background: rgba(38,139,210,0.04); border-radius: 0 var(--radius) var(--radius) 0; }
.prep-markdown .md-body hr { border: none; border-top: 1px solid var(--base2); margin: 16px 0; }

/* Form styles for prep modal */
.form-label { display: block; font-weight: 600; font-size: 13px; color: var(--base01); margin-bottom: 4px; }
.form-input { width: 100%; padding: 8px 10px; border: 1px solid var(--base2); border-radius: var(--radius); font-size: 13px; font-family: inherit; }
.form-input:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px rgba(38,139,210,0.15); }
select.form-input { appearance: auto; }
textarea.form-input { resize: vertical; }
```

- [ ] **Step 4: Test the full web flow**

1. Start the web server: `uv run python scripts/job_web.py`
2. Navigate to `http://127.0.0.1:8420/#/job/<id>` for a job with full pipeline output
3. Click the "Interview Prep" tab — should show "No interview prep guide generated yet" with a Generate button
4. Click "Generate Interview Prep" — modal should appear with stage dropdown, interviewer textarea, notes textarea
5. Click Generate — should close modal, show toast, tab should show spinner with progress
6. Wait for completion (5-15 min) — tab should auto-refresh and show the rendered markdown with download links
7. Click Download .docx and .pdf — both should download correctly
8. Click Regenerate — should open modal again and overwrite

- [ ] **Step 5: Commit**

```bash
git add scripts/static/index.html scripts/static/app.js scripts/static/style.css
git commit -m "feat: interview prep web UI — tab, modal, polling, markdown rendering"
```

---

## Task 6: Lint, test, and final integration

**Files:**
- All modified files

- [ ] **Step 1: Run linter**

```bash
uv run ruff check scripts/generate_interview_prep.py scripts/job_web.py
```

Fix any issues.

- [ ] **Step 2: Run existing tests to verify no regressions**

```bash
uv run python -m pytest tests/ -v --timeout=120
```

All previously-passing tests should still pass.

- [ ] **Step 3: Run architecture check**

```bash
uv run python scripts/check_architecture.py
```

- [ ] **Step 4: Update docs/output-structure.md**

Add the `interview_prep/` subdirectory documentation:

```markdown
## Interview Prep (optional, on-demand)

  interview_prep/
    interview_prep.md                         # Full prep guide (markdown)
    Jerrison Li Interview Prep - {Company}.docx  # Formatted Word document
    Jerrison Li Interview Prep - {Company}.pdf   # PDF version
    .progress.json                            # Generation progress (transient)
    .generating                               # Lock file with PID (transient)
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: interview prep guide — complete implementation

Adds interview preparation guide generation:
- CLI: uv run python scripts/generate_interview_prep.py <output_dir>
- Web: Interview Prep tab on job detail page with generate modal
- Output: markdown + docx + pdf in interview_prep/ subdirectory
- Uses Claude CLI subprocess with web research for deep company/role analysis
- 9-section guide: exec summary, company intel, interview format, interviewer
  profiles, behavioral/product/execution questions, questions to ask, prep strategy"
```
