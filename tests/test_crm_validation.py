"""Tests for robothor.crm.validation — blocklists and input validation (pure unit tests)."""

from robothor.crm.validation import (
    COMPANY_BLOCKLIST,
    PERSON_BLOCKLIST,
    normalize_email,
    scrub_null_string,
    validate_company_input,
    validate_person_input,
)


class TestScrubNullString:
    def test_none_returns_none(self):
        assert scrub_null_string(None) is None

    def test_null_string(self):
        assert scrub_null_string("null") == ""

    def test_none_string(self):
        assert scrub_null_string("None") == ""

    def test_na_string(self):
        assert scrub_null_string("N/A") == ""

    def test_normal_string(self):
        assert scrub_null_string("Hello") == "Hello"

    def test_whitespace_null(self):
        assert scrub_null_string("  null  ") == ""


class TestValidatePersonInput:
    def test_valid_person(self):
        valid, reason = validate_person_input("Philip", "Ironsail")
        assert valid is True
        assert reason == "ok"

    def test_blocked_furniture(self):
        valid, reason = validate_person_input("couch", "")
        assert valid is False
        assert "blocklist" in reason

    def test_blocked_full_name(self):
        valid, reason = validate_person_input("vision monitor", "system")
        assert valid is False

    def test_blocked_bot(self):
        valid, reason = validate_person_input("claude", "")
        assert valid is False

    def test_null_name_rejected(self):
        valid, reason = validate_person_input("null", "")
        assert valid is False
        assert "null-like" in reason

    def test_short_name_rejected(self):
        valid, reason = validate_person_input("a", "")
        assert valid is False
        assert "2 characters" in reason

    def test_invalid_email(self):
        valid, reason = validate_person_input("Philip", "I", email="not-an-email")
        assert valid is False
        assert "@" in reason

    def test_valid_with_email(self):
        valid, _ = validate_person_input("Philip", "Ironsail", email="philip@example.com")
        assert valid is True

    def test_none_email_ok(self):
        valid, _ = validate_person_input("Philip", "Ironsail", email=None)
        assert valid is True


class TestValidateCompanyInput:
    def test_valid_company(self):
        valid, _ = validate_company_input("Ironsail LLC")
        assert valid is True

    def test_blocked_null(self):
        valid, _ = validate_company_input("null")
        assert valid is False

    def test_blocked_unknown(self):
        valid, _ = validate_company_input("unknown")
        assert valid is False

    def test_short_name(self):
        valid, _ = validate_company_input("x")
        assert valid is False


class TestNormalizeEmail:
    def test_none(self):
        assert normalize_email(None) is None

    def test_empty(self):
        assert normalize_email("") is None

    def test_lowercase(self):
        assert normalize_email("Philip@Example.COM") == "philip@example.com"

    def test_strip_whitespace(self):
        assert normalize_email("  user@example.com  ") == "user@example.com"

    def test_no_at_sign(self):
        assert normalize_email("not-an-email") is None


class TestBlocklists:
    def test_person_blocklist_has_furniture(self):
        assert "chair" in PERSON_BLOCKLIST
        assert "table" in PERSON_BLOCKLIST

    def test_person_blocklist_has_bots(self):
        assert "claude" in PERSON_BLOCKLIST
        assert "email responder" in PERSON_BLOCKLIST

    def test_company_blocklist(self):
        assert "null" in COMPANY_BLOCKLIST
        assert "test" in COMPANY_BLOCKLIST

    def test_blocklists_are_sets(self):
        assert isinstance(PERSON_BLOCKLIST, set)
        assert isinstance(COMPANY_BLOCKLIST, set)
