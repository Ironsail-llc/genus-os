"""Tests for log sanitization utility."""

from robothor.engine.sanitize import sanitize_log


class TestSanitizeLog:
    def test_plain_string_unchanged(self):
        assert sanitize_log("hello world") == "hello world"

    def test_newline_escaped(self):
        assert sanitize_log("line1\nline2") == "line1\\nline2"

    def test_carriage_return_escaped(self):
        assert sanitize_log("line1\rline2") == "line1\\rline2"

    def test_crlf_escaped(self):
        assert sanitize_log("line1\r\nline2") == "line1\\r\\nline2"

    def test_all_c0_and_c1_controls_escaped(self):
        result = sanitize_log("value\x00\t\x7f\x85")
        assert result == "value\\x00\\x09\\x7f\\x85"
        assert all(ord(character) >= 0x20 for character in result)
        assert all(not 0x7F <= ord(character) < 0xA0 for character in result)

    def test_non_string_converted(self):
        assert sanitize_log(42) == "42"
        assert sanitize_log(None) == "None"

    def test_exception_sanitized(self):
        try:
            raise ValueError("bad\ninput")
        except ValueError as e:
            result = sanitize_log(e)
        assert "\\n" in result
        assert "\n" not in result
