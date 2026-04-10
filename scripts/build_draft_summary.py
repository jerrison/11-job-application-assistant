#!/usr/bin/env python3
"""CLI tool: render draft_summary.png from draft_summary.md using Pillow.

Usage:
    uv run python scripts/build_draft_summary.py <draft_summary.md> [-o <output.png>]

Reads the markdown, parses header and Q&A fields, and renders a formatted PNG
with green badges for "filled" fields and red for "unfilled".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("WARNING: Pillow not installed — cannot generate draft_summary.png", file=sys.stderr)
    sys.exit(0)


# ── Constants ──────────────────────────────────────────────────────────────

WIDTH = 800
PADDING_X = 30
PADDING_Y = 20
LINE_SPACING = 4
SECTION_SPACING = 14

BG_COLOR = (255, 255, 255)
TEXT_COLOR = (30, 30, 30)
HEADER_COLOR = (20, 20, 60)
SUBHEADER_COLOR = (80, 80, 80)
BADGE_FILLED = (34, 139, 34)  # Forest green
BADGE_UNFILLED = (200, 40, 40)  # Red
BADGE_PENDING = (38, 139, 210)
BADGE_UNKNOWN = (120, 120, 120)
BADGE_NOT_APPLICABLE = (181, 137, 0)
BADGE_TEXT_COLOR = (255, 255, 255)
FIELD_NAME_COLOR = (60, 60, 120)
META_COLOR = (120, 120, 120)
DIVIDER_COLOR = (200, 200, 200)
REFRESH_CARD_BG = (248, 248, 246)
REFRESH_CARD_BORDER = (214, 214, 208)


# ── Markdown parser ───────────────────────────────────────────────────────

_TITLE_RE = re.compile(r"^#\s+Draft:\s+(.+?)\s+—\s+(.+)$", re.MULTILINE)
_BOARD_RE = re.compile(r"\*\*Board:\*\*\s+(\S+)\s+\|\s+\*\*Generated:\*\*\s+(.+)$", re.MULTILINE)
_FIELD_HEADER_RE = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s+\((\S+)\)\s*$", re.MULTILINE)
_KIND_RE = re.compile(r"\*\*Kind:\*\*\s+(\S+)\s+\|\s+\*\*Required:\*\*\s+(\S+)")
_ANSWER_RE = re.compile(r"\*\*Answer:\*\*\s*(.*)")
_STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(\S+)")
_LINKED_RESOURCE_RE = re.compile(r"\*\*Linked Resource:\*\*\s*(.*)")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_DETAIL_LINE_RE = re.compile(r"^- \*\*([^:]+):\*\*\s*(.*)$", re.MULTILINE)
_DETAIL_BLOCK_STOP_RE = re.compile(r"^(?:##\s+|###\s+|- \*\*[^:]+:\*\*)")


def _extract_section(text: str, heading: str) -> str | None:
    sections = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(sections):
        if match.group(1).strip() != heading:
            continue
        start = match.end()
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        return text[start:end]
    return None


def _default_answer_refresh_message(status: str) -> str:
    messages = {
        "pending": "Waiting for fresh answer generation proof.",
        "fresh": "Fresh answer generation proof recorded.",
        "not_applicable": "No generated application answers were present for this draft.",
        "failed": "Answer regeneration failed before fresh proof was recorded.",
        "unknown": "This draft predates the answer refresh proof contract.",
    }
    return messages.get(status, "Answer refresh state is not available.")


def _parse_answer_refresh(text: str) -> dict | None:
    block = _extract_section(text, "Answer Refresh")
    if block is None:
        return None
    refresh = {
        "status": "unknown",
        "request": None,
        "message": None,
        "requested": None,
        "resolved": None,
        "provider": None,
        "answer_generated": None,
        "generated_answers": None,
    }
    key_map = {
        "status": "status",
        "request": "request",
        "message": "message",
        "requested": "requested",
        "resolved": "resolved",
        "provider": "provider",
        "answer generated": "answer_generated",
        "generated answers": "generated_answers",
    }
    for match in _DETAIL_LINE_RE.finditer(block):
        source_key = match.group(1).strip().lower()
        mapped_key = key_map.get(source_key)
        if mapped_key is None:
            continue
        value = match.group(2).strip() or None
        refresh[mapped_key] = value
    if not refresh["message"]:
        refresh["message"] = _default_answer_refresh_message(refresh["status"])
    return refresh


def _extract_detail_block(block: str, label: str) -> str | None:
    prefix = f"- **{label}:**"
    values: list[str] = []
    capture = False

    for line in block.splitlines():
        if not capture:
            if line.startswith(prefix):
                capture = True
                values.append(line[len(prefix) :].strip())
            continue

        if _DETAIL_BLOCK_STOP_RE.match(line):
            break
        values.append(line.strip())

    if not capture:
        return None

    while values and not values[-1]:
        values.pop()

    value = "\n".join(values).strip()
    return value or None


def _parse_md(text: str) -> dict:
    """Parse draft_summary.md into structured data."""
    result: dict = {
        "role": "Unknown",
        "company": "Unknown",
        "board": "?",
        "generated": "?",
        "fields": [],
        "answer_refresh": None,
    }

    m = _TITLE_RE.search(text)
    if m:
        result["role"] = m.group(1).strip()
        result["company"] = m.group(2).strip()

    m = _BOARD_RE.search(text)
    if m:
        result["board"] = m.group(1).strip()
        result["generated"] = m.group(2).strip()

    result["answer_refresh"] = _parse_answer_refresh(text)

    # Split by field headers
    field_starts = list(_FIELD_HEADER_RE.finditer(text))
    for i, fm in enumerate(field_starts):
        end = field_starts[i + 1].start() if i + 1 < len(field_starts) else len(text)
        block = text[fm.start() : end]

        field: dict = {
            "num": fm.group(1),
            "label": fm.group(2),
            "field_name": fm.group(3),
            "kind": "text",
            "required": "no",
            "answer": "\u2014",
            "status": "unfilled",
            "linked_resource": None,
        }

        km = _KIND_RE.search(block)
        if km:
            field["kind"] = km.group(1)
            field["required"] = km.group(2)

        answer = _extract_detail_block(block, "Answer")
        if answer is not None:
            field["answer"] = answer

        sm = _STATUS_RE.search(block)
        if sm:
            field["status"] = sm.group(1)
        linked_resource = _extract_detail_block(block, "Linked Resource")
        if linked_resource is not None:
            field["linked_resource"] = linked_resource

        result["fields"].append(field)

    return result


# ── Rendering ─────────────────────────────────────────────────────────────


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a system font; fall back to Pillow default."""
    candidates = []
    if bold:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSText-Bold.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSText.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Pillow default
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _get_mono_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a monospace font."""
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFMono-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Word-wrap text to fit within max_width."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip() if current else word
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines or [""]


def _answer_refresh_badge(status: str) -> tuple[str, tuple[int, int, int]]:
    labels = {
        "pending": ("PENDING", BADGE_PENDING),
        "fresh": ("FRESH", BADGE_FILLED),
        "not_applicable": ("N/A", BADGE_NOT_APPLICABLE),
        "failed": ("FAILED", BADGE_UNFILLED),
        "unknown": ("LEGACY", BADGE_UNKNOWN),
    }
    return labels.get(status, ("UNKNOWN", BADGE_UNKNOWN))


def _answer_refresh_lines(refresh: dict | None) -> list[str]:
    if not refresh:
        return []
    lines = []
    if refresh.get("request"):
        lines.append(f"Request: {refresh['request']}")
    if refresh.get("requested"):
        lines.append(f"Requested: {refresh['requested']}")
    if refresh.get("resolved"):
        lines.append(f"Resolved: {refresh['resolved']}")
    if refresh.get("provider"):
        lines.append(f"Provider: {refresh['provider']}")
    if refresh.get("answer_generated"):
        lines.append(f"Answer generated: {refresh['answer_generated']}")
    if refresh.get("generated_answers") is not None:
        lines.append(f"Generated answers: {refresh['generated_answers']}")
    lines.append(
        f"Message: {refresh.get('message') or _default_answer_refresh_message(refresh.get('status', 'unknown'))}"
    )
    return lines


def _measure_answer_refresh_card(
    draw: ImageDraw.ImageDraw,
    refresh: dict | None,
    *,
    width: int,
    font_section: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_body: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_badge: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, list[list[str]]]:
    if not refresh:
        return 0, []

    badge_text, _badge_color = _answer_refresh_badge(refresh.get("status", "unknown"))
    badge_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    badge_h = badge_bbox[3] - badge_bbox[1] + 6
    title_h = _text_height(draw, "Answer Refresh", font_section)

    wrapped_lines: list[list[str]] = []
    content_height = max(title_h, badge_h) + LINE_SPACING
    for line in _answer_refresh_lines(refresh):
        wraps = _wrapped_text(draw, line, font_body, width - 28)
        wrapped_lines.append(wraps)
        for wrapped in wraps:
            content_height += _text_height(draw, wrapped, font_body) + LINE_SPACING
    card_height = content_height + 24
    return card_height, wrapped_lines


def render_png(data: dict, output_path: Path) -> None:
    """Render parsed draft data to a PNG image."""
    # Phase 1: calculate required height
    font_title = _get_font(22, bold=True)
    font_subtitle = _get_font(13)
    font_field_label = _get_font(14, bold=True)
    font_field_name = _get_mono_font(11)
    font_body = _get_font(12)
    font_badge = _get_font(11, bold=True)
    font_section = _get_font(16, bold=True)

    # Create a temp image for text measurement
    tmp_img = Image.new("RGB", (WIDTH, 100))
    tmp_draw = ImageDraw.Draw(tmp_img)

    content_width = WIDTH - 2 * PADDING_X
    y = PADDING_Y

    # Title
    y += _text_height(tmp_draw, data["role"], font_title) + LINE_SPACING
    # Subtitle (board + generated)
    y += _text_height(tmp_draw, "Board", font_subtitle) + SECTION_SPACING
    # Divider
    y += 2 + SECTION_SPACING

    # Stats line
    filled = sum(1 for f in data["fields"] if f["status"] == "filled")
    total = len(data["fields"])
    y += _text_height(tmp_draw, "Stats", font_body) + SECTION_SPACING

    refresh_card_height, _refresh_wrapped_lines = _measure_answer_refresh_card(
        tmp_draw,
        data.get("answer_refresh"),
        width=content_width,
        font_section=font_section,
        font_body=font_body,
        font_badge=font_badge,
    )
    if refresh_card_height:
        y += refresh_card_height + SECTION_SPACING

    # Section header
    y += _text_height(tmp_draw, "Application Answers", font_section) + SECTION_SPACING

    # Fields
    for field in data["fields"]:
        # Field header line (label + field_name + badge)
        y += _text_height(tmp_draw, field["label"], font_field_label) + LINE_SPACING
        # Meta line (kind, required)
        y += _text_height(tmp_draw, "kind", font_body) + LINE_SPACING
        # Answer (possibly wrapped)
        answer_lines = _wrapped_text(tmp_draw, field["answer"], font_body, content_width - 60)
        for _ in answer_lines:
            y += _text_height(tmp_draw, "A", font_body) + LINE_SPACING
        if field.get("linked_resource"):
            linked_lines = _wrapped_text(tmp_draw, str(field["linked_resource"]), font_body, content_width - 60)
            for _ in linked_lines:
                y += _text_height(tmp_draw, "A", font_body) + LINE_SPACING
        y += SECTION_SPACING

    y += PADDING_Y  # Bottom padding

    # Phase 2: render
    img = Image.new("RGB", (WIDTH, y), BG_COLOR)
    draw = ImageDraw.Draw(img)

    cy = PADDING_Y

    # Title
    title_text = f"{data['role']} -- {data['company']}"
    draw.text((PADDING_X, cy), title_text, fill=HEADER_COLOR, font=font_title)
    cy += _text_height(draw, title_text, font_title) + LINE_SPACING

    # Subtitle
    sub_text = f"Board: {data['board']}    Generated: {data['generated']}"
    draw.text((PADDING_X, cy), sub_text, fill=SUBHEADER_COLOR, font=font_subtitle)
    cy += _text_height(draw, sub_text, font_subtitle) + SECTION_SPACING

    # Divider
    draw.line([(PADDING_X, cy), (WIDTH - PADDING_X, cy)], fill=DIVIDER_COLOR, width=1)
    cy += 2 + SECTION_SPACING

    # Stats
    unfilled = total - filled
    stats_text = f"{filled} filled / {unfilled} unfilled / {total} total"
    draw.text((PADDING_X, cy), stats_text, fill=TEXT_COLOR, font=font_body)
    cy += _text_height(draw, stats_text, font_body) + SECTION_SPACING

    refresh = data.get("answer_refresh")
    refresh_card_height, refresh_wrapped_lines = _measure_answer_refresh_card(
        draw,
        refresh,
        width=content_width,
        font_section=font_section,
        font_body=font_body,
        font_badge=font_badge,
    )
    if refresh_card_height:
        badge_text, badge_color = _answer_refresh_badge(refresh.get("status", "unknown"))
        card_top = cy
        card_bottom = cy + refresh_card_height
        draw.rounded_rectangle(
            [(PADDING_X, card_top), (WIDTH - PADDING_X, card_bottom)],
            radius=10,
            fill=REFRESH_CARD_BG,
            outline=REFRESH_CARD_BORDER,
            width=1,
        )

        inner_x = PADDING_X + 14
        inner_y = card_top + 12
        draw.text((inner_x, inner_y), "Answer Refresh", fill=HEADER_COLOR, font=font_section)

        badge_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
        badge_w = badge_bbox[2] - badge_bbox[0] + 12
        badge_h = badge_bbox[3] - badge_bbox[1] + 6
        badge_x = WIDTH - PADDING_X - 14 - badge_w
        badge_y = inner_y
        draw.rounded_rectangle(
            [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
            radius=4,
            fill=badge_color,
        )
        draw.text((badge_x + 6, badge_y + 3), badge_text, fill=BADGE_TEXT_COLOR, font=font_badge)

        inner_y += max(_text_height(draw, "Answer Refresh", font_section), badge_h) + LINE_SPACING
        for wrapped_group in refresh_wrapped_lines:
            for wrapped in wrapped_group:
                draw.text((inner_x, inner_y), wrapped, fill=TEXT_COLOR, font=font_body)
                inner_y += _text_height(draw, wrapped, font_body) + LINE_SPACING

        cy = card_bottom + SECTION_SPACING

    # Section header
    draw.text((PADDING_X, cy), "Application Answers", fill=HEADER_COLOR, font=font_section)
    cy += _text_height(draw, "Application Answers", font_section) + SECTION_SPACING

    # Fields
    for field in data["fields"]:
        # Badge
        badge_text = field["status"].upper()
        badge_color = BADGE_FILLED if field["status"] == "filled" else BADGE_UNFILLED
        badge_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
        badge_w = badge_bbox[2] - badge_bbox[0] + 12
        badge_h = badge_bbox[3] - badge_bbox[1] + 6

        # Draw badge
        badge_x = WIDTH - PADDING_X - badge_w
        badge_y = cy
        draw.rounded_rectangle(
            [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
            radius=4,
            fill=badge_color,
        )
        draw.text((badge_x + 6, badge_y + 3), badge_text, fill=BADGE_TEXT_COLOR, font=font_badge)

        # Field label + field_name
        label_text = f"{field['num']}. {field['label']}"
        draw.text((PADDING_X, cy), label_text, fill=TEXT_COLOR, font=font_field_label)
        label_h = _text_height(draw, label_text, font_field_label)

        # Field name (monospace, next to label)
        fname_text = f"  ({field['field_name']})"
        label_bbox = draw.textbbox((PADDING_X, cy), label_text, font=font_field_label)
        draw.text((label_bbox[2], cy + 2), fname_text, fill=FIELD_NAME_COLOR, font=font_field_name)

        cy += max(label_h, badge_h) + LINE_SPACING

        # Meta
        meta_text = f"Kind: {field['kind']}  |  Required: {field['required']}"
        draw.text((PADDING_X + 16, cy), meta_text, fill=META_COLOR, font=font_body)
        cy += _text_height(draw, meta_text, font_body) + LINE_SPACING

        # Answer (wrapped)
        answer_lines = _wrapped_text(draw, field["answer"], font_body, content_width - 60)
        for line in answer_lines:
            draw.text((PADDING_X + 16, cy), line, fill=TEXT_COLOR, font=font_body)
            cy += _text_height(draw, line, font_body) + LINE_SPACING
        if field.get("linked_resource"):
            linked_lines = _wrapped_text(draw, str(field["linked_resource"]), font_body, content_width - 60)
            for line in linked_lines:
                draw.text((PADDING_X + 16, cy), line, fill=META_COLOR, font=font_body)
                cy += _text_height(draw, line, font_body) + LINE_SPACING

        cy += SECTION_SPACING

    img.save(output_path, "PNG")


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Render draft_summary.png from draft_summary.md")
    parser.add_argument("md_path", type=Path, help="Path to draft_summary.md")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output PNG path (default: same dir as input)")
    args = parser.parse_args()

    md_path: Path = args.md_path
    if not md_path.exists():
        print(f"ERROR: {md_path} not found", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or md_path.parent / "draft_summary.png"

    text = md_path.read_text(encoding="utf-8")
    data = _parse_md(text)
    render_png(data, output_path)
    print(f"Generated: {output_path}")


if __name__ == "__main__":
    main()
