"""Unit tests for safe_codefence_content helper (B1)."""
from __future__ import annotations

from bot_modules.services.whisper_service import safe_codefence_content


def test_plain_string_unchanged():
    assert safe_codefence_content("hello") == "hello"


def test_single_triple_backtick_replaced():
    assert "```" not in safe_codefence_content("hi ```code```")


def test_replacement_uses_homoglyphs():
    result = safe_codefence_content("hi ```code```")
    assert "ʼʼʼ" in result
    assert result == "hi ʼʼʼcodeʼʼʼ"


def test_multiple_triple_backticks_all_replaced():
    result = safe_codefence_content("``` ``` ```")
    assert "```" not in result
    assert result.count("ʼʼʼ") == 3


def test_empty_string_unchanged():
    assert safe_codefence_content("") == ""


def test_two_backticks_not_affected():
    assert safe_codefence_content("``foo``") == "``foo``"
