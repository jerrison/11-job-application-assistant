#!/usr/bin/env python3
"""Helpers for writing XML-safe text into DOCX documents."""

from __future__ import annotations


def _is_xml_compatible_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        codepoint in (0x09, 0x0A, 0x0D)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def sanitize_docx_text(text: object) -> str:
    """Drop XML-invalid control characters before handing text to python-docx."""
    value = "" if text is None else str(text)
    if not value:
        return ""
    return "".join(char for char in value if _is_xml_compatible_char(char))
