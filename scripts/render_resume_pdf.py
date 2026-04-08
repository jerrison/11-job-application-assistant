#!/usr/bin/env python3
"""Render master_resume.md into a two-page resume PDF matching template format."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer

INPUT_MD = Path("master_resume.md")
OUTPUT_PDF = Path("output/pdf/master_resume.pdf")
TMP_FIT_PDF = Path("tmp/pdfs/master_resume_fit_check.pdf")
MAX_PAGES = 2

MONTH_TOKEN_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b")
DATE_TOKEN_RE = re.compile(r"\b(?:19|20)\d{2}\b|\bPresent\b")


@dataclass
class ExperienceRole:
    title: str
    meta: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class ResumeModel:
    name: str
    title: str
    contact: str
    experience: list[ExperienceRole]
    education_lines: list[str]
    skills_lines: list[str]


@dataclass
class FontSet:
    regular: str
    bold: str
    italic: str
    bold_italic: str


def normalize_text(value: str) -> str:
    # Keep text consistent while preserving template-style em/en dashes.
    return value.replace("‑", "-").replace("“", '"').replace("”", '"').replace("’", "'")


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def register_fonts() -> FontSet:
    carlito_paths = {
        "regular": Path("assets/fonts/Carlito-Regular.ttf"),
        "bold": Path("assets/fonts/Carlito-Bold.ttf"),
        "italic": Path("assets/fonts/Carlito-Italic.ttf"),
        "bold_italic": Path("assets/fonts/Carlito-BoldItalic.ttf"),
    }
    if all(path.exists() for path in carlito_paths.values()):
        pdfmetrics.registerFont(TTFont("ResumeCarlito", str(carlito_paths["regular"])))
        pdfmetrics.registerFont(TTFont("ResumeCarlitoBold", str(carlito_paths["bold"])))
        pdfmetrics.registerFont(TTFont("ResumeCarlitoItalic", str(carlito_paths["italic"])))
        pdfmetrics.registerFont(TTFont("ResumeCarlitoBoldItalic", str(carlito_paths["bold_italic"])))
        addMapping("ResumeCarlito", 0, 0, "ResumeCarlito")
        addMapping("ResumeCarlito", 1, 0, "ResumeCarlitoBold")
        addMapping("ResumeCarlito", 0, 1, "ResumeCarlitoItalic")
        addMapping("ResumeCarlito", 1, 1, "ResumeCarlitoBoldItalic")
        return FontSet(
            regular="ResumeCarlito",
            bold="ResumeCarlitoBold",
            italic="ResumeCarlitoItalic",
            bold_italic="ResumeCarlitoBoldItalic",
        )

    arial_paths = {
        "regular": Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        "bold": Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        "italic": Path("/System/Library/Fonts/Supplemental/Arial Italic.ttf"),
    }
    if all(path.exists() for path in arial_paths.values()):
        pdfmetrics.registerFont(TTFont("ResumeArial", str(arial_paths["regular"])))
        pdfmetrics.registerFont(TTFont("ResumeArialBold", str(arial_paths["bold"])))
        pdfmetrics.registerFont(TTFont("ResumeArialItalic", str(arial_paths["italic"])))
        addMapping("ResumeArial", 0, 0, "ResumeArial")
        addMapping("ResumeArial", 1, 0, "ResumeArialBold")
        addMapping("ResumeArial", 0, 1, "ResumeArialItalic")
        addMapping("ResumeArial", 1, 1, "ResumeArialBold")
        return FontSet(
            regular="ResumeArial",
            bold="ResumeArialBold",
            italic="ResumeArialItalic",
            bold_italic="ResumeArialBold",
        )
    return FontSet(
        regular="Helvetica",
        bold="Helvetica-Bold",
        italic="Helvetica-Oblique",
        bold_italic="Helvetica-BoldOblique",
    )


def format_role_title(line: str) -> str:
    parts = re.split(r"\s+[—-]\s+", line, maxsplit=1)
    if len(parts) == 2:
        company, role = parts
        return f"<b>{escape_html(company)}</b> — {escape_html(role)}"
    return f"<b>{escape_html(line)}</b>"


def format_bullet_text(line: str) -> str:
    line = line.strip()

    first_period = line.find(". ")
    if 20 <= first_period <= 140:
        lead = line[: first_period + 1]
        rest = line[first_period + 2 :]
        if rest:
            return f"<b>{escape_html(lead)}</b> {escape_html(rest)}"
        return f"<b>{escape_html(lead)}</b>"

    by_pos = line.find(" by ")
    if 20 <= by_pos <= 120:
        lead, rest = line.split(" by ", 1)
        return f"<b>{escape_html(lead)}</b> by {escape_html(rest)}"

    return escape_html(line)


def is_meta_line(line: str) -> bool:
    return "|" in line and (DATE_TOKEN_RE.search(line) is not None or MONTH_TOKEN_RE.search(line) is not None)


def is_section_heading(line: str) -> bool:
    return line in {"EXPERIENCE", "EDUCATION", "SKILLS & ADDITIONAL"}


def load_resume_lines(path: Path) -> list[str]:
    raw = normalize_text(path.read_text(encoding="utf-8"))
    lines = [line.rstrip() for line in raw.splitlines()]

    if "---" in lines:
        divider = lines.index("---")
        lines = lines[divider + 1 :]

    return [line for line in lines if line.strip()]


def parse_experience(lines: list[str]) -> list[ExperienceRole]:
    roles: list[ExperienceRole] = []
    current: ExperienceRole | None = None

    for line in lines:
        if line.startswith("* "):
            if current is not None:
                current.bullets.append(line[2:].strip())
            continue

        if current is None:
            current = ExperienceRole(title=line.strip())
            continue

        if not current.meta and is_meta_line(line):
            current.meta = line.strip()
            continue

        if current.meta or current.bullets:
            roles.append(current)
            current = ExperienceRole(title=line.strip())
            continue

        current.title = f"{current.title} {line.strip()}"

    if current is not None:
        roles.append(current)

    return roles


def parse_resume_model(lines: list[str]) -> ResumeModel:
    if len(lines) < 3:
        raise ValueError("master_resume.md does not contain the expected header lines.")

    name, title, contact = lines[0], lines[1], lines[2]
    section_lines: dict[str, list[str]] = {"EXPERIENCE": [], "EDUCATION": [], "SKILLS & ADDITIONAL": []}
    section = "EXPERIENCE"

    for line in lines[3:]:
        if is_section_heading(line):
            section = line
            continue
        section_lines[section].append(line)

    return ResumeModel(
        name=name,
        title=title,
        contact=contact,
        experience=parse_experience(section_lines["EXPERIENCE"]),
        education_lines=section_lines["EDUCATION"],
        skills_lines=section_lines["SKILLS & ADDITIONAL"],
    )


def build_styles(fonts: FontSet) -> dict[str, ParagraphStyle]:
    styles = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "ResumeName",
            parent=styles["Normal"],
            fontName=fonts.bold,
            fontSize=22.0,
            leading=24.0,
            alignment=TA_CENTER,
            spaceAfter=7,
        ),
        "title": ParagraphStyle(
            "ResumeTitle",
            parent=styles["Normal"],
            fontName=fonts.regular,
            fontSize=11.0,
            leading=12.4,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1f2937"),
            spaceAfter=1.4,
        ),
        "contact": ParagraphStyle(
            "ResumeContact",
            parent=styles["Normal"],
            fontName=fonts.regular,
            fontSize=11.0,
            leading=12.4,
            alignment=TA_CENTER,
            spaceAfter=7,
        ),
        "section": ParagraphStyle(
            "ResumeSection",
            parent=styles["Normal"],
            fontName=fonts.bold,
            fontSize=14.0,
            leading=15.2,
            textColor=colors.HexColor("#2f5d9b"),
            spaceBefore=4.2,
            spaceAfter=1.1,
        ),
        "role_title": ParagraphStyle(
            "ResumeRoleTitle",
            parent=styles["Normal"],
            fontName=fonts.regular,
            fontSize=11.7,
            leading=13.2,
            spaceBefore=3.0,
            spaceAfter=0.5,
        ),
        "meta": ParagraphStyle(
            "ResumeMeta",
            parent=styles["Normal"],
            fontName=fonts.italic,
            fontSize=10.2,
            leading=11.9,
            textColor=colors.HexColor("#374151"),
            spaceAfter=0.8,
        ),
        "bullet": ParagraphStyle(
            "ResumeBullet",
            parent=styles["Normal"],
            fontName=fonts.regular,
            fontSize=11.2,
            leading=14.0,
            alignment=TA_JUSTIFY,
            leftIndent=12.8,
            bulletIndent=2.0,
            spaceAfter=2.4,
        ),
        "school": ParagraphStyle(
            "ResumeSchool",
            parent=styles["Normal"],
            fontName=fonts.bold,
            fontSize=11.5,
            leading=12.8,
            spaceBefore=2.2,
            spaceAfter=0.6,
        ),
        "line": ParagraphStyle(
            "ResumeLine",
            parent=styles["Normal"],
            fontName=fonts.regular,
            fontSize=10.8,
            leading=13.6,
            spaceAfter=2.7,
        ),
    }


def looks_like_school_heading(line: str) -> bool:
    return re.match(r"^[A-Z0-9 .,'()&/+:-]+$", line) is not None


def split_education_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if looks_like_school_heading(line) and current:
            blocks.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def parse_degree_line(line: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def compact_education_blocks(blocks: list[list[str]]) -> list[list[str]]:
    if len(blocks) < 2 or len(blocks[0]) < 2 or len(blocks[1]) < 2:
        return blocks

    heading_1, heading_2 = blocks[0][0], blocks[1][0]
    if "WHARTON SCHOOL" not in heading_1 or "PENN ENGINEERING" not in heading_2:
        return blocks

    degree_1 = parse_degree_line(blocks[0][1])
    degree_2 = parse_degree_line(blocks[1][1])
    if degree_1 is None or degree_2 is None:
        return blocks

    if degree_1[1] != degree_2[1] or degree_1[2] != degree_2[2]:
        return blocks

    first_extras = blocks[0][2:]
    second_extras = blocks[1][2:]
    gmat_lines = [line for line in first_extras if "GMAT" in line]
    narrative_1 = [line for line in first_extras if "GMAT" not in line]
    narrative_2 = second_extras
    narrative = " ".join(narrative_1 + narrative_2).strip()

    merged = [
        "THE WHARTON SCHOOL & PENN ENGINEERING, UNIVERSITY OF PENNSYLVANIA",
        f"{degree_1[0]} & {degree_2[0]} | {degree_1[1]} | {degree_1[2]}",
    ]
    if narrative:
        merged.append(narrative)
    merged.extend(gmat_lines)

    return [merged] + blocks[2:]


def format_education_line(line: str) -> str:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) == 3:
        left = escape_html(parts[0])
        right = " | ".join(f"<i>{escape_html(part)}</i>" for part in parts[1:])
        return f"{left} | {right}"
    return escape_html(line)


def format_skills_line(line: str) -> str:
    segments = [seg.strip() for seg in line.split("|")]
    rendered: list[str] = []
    for seg in segments:
        if ":" in seg:
            label, value = seg.split(":", 1)
            rendered.append(f"<b>{escape_html(label.strip())}:</b> {escape_html(value.strip())}")
        else:
            rendered.append(escape_html(seg))
    return " | ".join(rendered)


def build_story(model: ResumeModel, bullet_limits: dict[int, int], fonts: FontSet) -> list:
    styles = build_styles(fonts)
    section_rule = dict(
        width="100%",
        thickness=1.0,
        color=colors.HexColor("#2f5d9b"),
        lineCap="round",
        spaceBefore=0,
        spaceAfter=2,
    )

    story: list = [
        Paragraph(escape_html(model.name), styles["name"]),
        Paragraph(escape_html(model.title), styles["title"]),
        Paragraph(escape_html(model.contact), styles["contact"]),
        Spacer(1, 0.0 * inch),
        Paragraph("EXPERIENCE", styles["section"]),
        HRFlowable(**section_rule),
        Spacer(1, 0.09 * inch),
    ]

    role_blocks: list[list] = []
    for idx, role in enumerate(model.experience):
        role_items = [Paragraph(format_role_title(role.title), styles["role_title"])]
        if role.meta:
            role_items.append(Paragraph(escape_html(role.meta), styles["meta"]))

        limit = bullet_limits.get(idx, len(role.bullets))
        for bullet in role.bullets[:limit]:
            role_items.append(Paragraph(format_bullet_text(bullet), styles["bullet"], bulletText="•"))
        role_blocks.append(role_items)

    # Keep a trailing cluster of short roles together (e.g., LYFT + ALLSTATE)
    # so they don't leave an orphan role at the end of page 1.
    tail_start = len(role_blocks)
    for idx in range(len(role_blocks) - 1, -1, -1):
        if bullet_limits.get(idx, 0) <= 1:
            tail_start = idx
            continue
        break
    tail_count = len(role_blocks) - tail_start

    if tail_count >= 2:
        for idx in range(tail_start):
            story.append(KeepTogether(role_blocks[idx]))
            if idx < tail_start - 1:
                story.append(Spacer(1, 0.06 * inch))
        story.append(PageBreak())
        for idx in range(tail_start, len(role_blocks)):
            story.append(KeepTogether(role_blocks[idx]))
            if idx < len(role_blocks) - 1:
                story.append(Spacer(1, 0.06 * inch))
    else:
        for idx, block in enumerate(role_blocks):
            story.append(KeepTogether(block))
            if idx < len(role_blocks) - 1:
                story.append(Spacer(1, 0.06 * inch))

    story.append(Spacer(1, 0.12 * inch))
    story.append(Paragraph("EDUCATION", styles["section"]))
    story.append(HRFlowable(**section_rule))
    education_blocks = compact_education_blocks(split_education_blocks(model.education_lines))
    for block in education_blocks:
        if not block:
            continue
        edu_items = [Paragraph(escape_html(block[0]), styles["school"])]
        for line in block[1:]:
            edu_items.append(Paragraph(format_education_line(line), styles["line"]))
        story.append(KeepTogether(edu_items))

    story.append(Spacer(1, 0.04 * inch))
    story.append(Paragraph("SKILLS &amp; ADDITIONAL", styles["section"]))
    story.append(HRFlowable(**section_rule))
    for line in model.skills_lines:
        story.append(Paragraph(format_skills_line(line), styles["line"]))

    return story


def soft_min_bullets(role_index: int) -> int:
    if role_index == 0:
        return 4
    if role_index == 1:
        return 3
    if role_index == 2:
        return 2
    return 1


def hard_min_bullets(role_index: int) -> int:
    if role_index == 0:
        return 2
    return 0


def render_pdf(model: ResumeModel, bullet_limits: dict[int, int], output_path: Path, fonts: FontSet) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.42 * inch,
        rightMargin=0.42 * inch,
        topMargin=0.42 * inch,
        bottomMargin=0.42 * inch,
        title="Master Resume",
        author="Jerrison Li",
    )
    doc.build(build_story(model, bullet_limits, fonts))


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def fit_bullet_limits(model: ResumeModel, fonts: FontSet) -> tuple[dict[int, int], int]:
    limits = {idx: len(role.bullets) for idx, role in enumerate(model.experience)}
    phase = "soft"

    while True:
        render_pdf(model, limits, TMP_FIT_PDF, fonts)
        pages = page_count(TMP_FIT_PDF)
        if pages <= MAX_PAGES:
            return limits, pages

        chosen_idx: int | None = None
        for idx in range(len(model.experience) - 1, -1, -1):
            min_keep = soft_min_bullets(idx) if phase == "soft" else hard_min_bullets(idx)
            if limits[idx] > min_keep:
                chosen_idx = idx
                break

        if chosen_idx is not None:
            limits[chosen_idx] -= 1
            continue

        if phase == "soft":
            phase = "hard"
            continue

        raise RuntimeError("Unable to fit resume content into two pages.")


def main() -> None:
    lines = load_resume_lines(INPUT_MD)
    model = parse_resume_model(lines)
    fonts = register_fonts()
    limits, pages = fit_bullet_limits(model, fonts)
    render_pdf(model, limits, OUTPUT_PDF, fonts)

    print(f"Rendered {OUTPUT_PDF} ({pages} pages).")
    for idx, role in enumerate(model.experience, start=1):
        kept = limits[idx - 1]
        total = len(role.bullets)
        print(f"Role {idx}: kept {kept}/{total} bullets - {role.title}")


if __name__ == "__main__":
    main()
