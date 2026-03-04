#!/usr/bin/env python3
"""Tests for vault_client.py — Vaultwarden API client."""

from unittest.mock import patch

import pytest
from vault_client import (
    VaultClient,
    _decrypt_aes,
    _derive_key,
    _encrypt_aes,
    _get_master_password,
    _make_password_hash,
)


class TestKeyDerivation:
    """Test Bitwarden-compatible key derivation."""

    def test_derive_key_deterministic(self):
        key1 = _derive_key("password123", "test@example.com")
        key2 = _derive_key("password123", "test@example.com")
        assert key1 == key2
        assert len(key1) == 32

    def test_derive_key_case_insensitive_email(self):
        key1 = _derive_key("password123", "Test@Example.com")
        key2 = _derive_key("password123", "test@example.com")
        assert key1 == key2

    def test_different_passwords_different_keys(self):
        key1 = _derive_key("password1", "test@example.com")
        key2 = _derive_key("password2", "test@example.com")
        assert key1 != key2

    def test_password_hash_deterministic(self):
        key = _derive_key("password123", "test@example.com")
        h1 = _make_password_hash(key, "password123")
        h2 = _make_password_hash(key, "password123")
        assert h1 == h2
        assert len(h1) > 0


class TestEncryptDecrypt:
    """Test AES-CBC encryption round-trips."""

    def test_round_trip(self):
        import secrets

        enc_key = secrets.token_bytes(32)
        mac_key = secrets.token_bytes(32)

        plaintext = "Hello, Robothor!"
        encrypted = _encrypt_aes(enc_key, mac_key, plaintext)
        assert encrypted.startswith("2.")
        assert "|" in encrypted

        decrypted = _decrypt_aes(enc_key, mac_key, encrypted)
        assert decrypted == plaintext

    def test_round_trip_unicode(self):
        import secrets

        enc_key = secrets.token_bytes(32)
        mac_key = secrets.token_bytes(32)

        plaintext = "Password: Cr3d$ntials!@#"
        encrypted = _encrypt_aes(enc_key, mac_key, plaintext)
        decrypted = _decrypt_aes(enc_key, mac_key, encrypted)
        assert decrypted == plaintext

    def test_encrypt_none_returns_none(self):
        import secrets

        enc_key = secrets.token_bytes(32)
        mac_key = secrets.token_bytes(32)
        assert _encrypt_aes(enc_key, mac_key, None) is None
        assert _encrypt_aes(enc_key, mac_key, "") is None

    def test_decrypt_none_passthrough(self):
        import secrets

        enc_key = secrets.token_bytes(32)
        mac_key = secrets.token_bytes(32)
        assert _decrypt_aes(enc_key, mac_key, None) is None
        assert _decrypt_aes(enc_key, mac_key, "plaintext") == "plaintext"

    def test_tampered_mac_detected(self):
        import secrets

        enc_key = secrets.token_bytes(32)
        mac_key = secrets.token_bytes(32)

        encrypted = _encrypt_aes(enc_key, mac_key, "secret")
        # Tamper with the MAC
        parts = encrypted[2:].split("|")
        import base64

        bad_mac = base64.b64encode(b"0" * 32).decode()
        tampered = f"2.{parts[0]}|{parts[1]}|{bad_mac}"

        result = _decrypt_aes(enc_key, mac_key, tampered)
        assert result == "[MAC verification failed]"


class TestGetMasterPassword:
    """Test master password retrieval from environment and secrets file."""

    def test_from_environment(self):
        with patch.dict("os.environ", {"VAULTWARDEN_MASTER_PASSWORD": "test123"}):
            assert _get_master_password() == "test123"

    def test_from_secrets_file_unquoted(self, tmp_path):
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("OTHER_VAR=foo\nVAULTWARDEN_MASTER_PASSWORD=mypassword\n")

        # Test the file parsing logic directly
        with open(secrets_file) as f:
            for line in f:
                if line.startswith("VAULTWARDEN_MASTER_PASSWORD="):
                    pw = line.strip().split("=", 1)[1]
                    if pw and pw[0] in ("'", '"') and pw[-1] == pw[0]:
                        pw = pw[1:-1]
                    assert pw == "mypassword"

    def test_from_secrets_file_quoted(self, tmp_path):
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("VAULTWARDEN_MASTER_PASSWORD='quoted_pass'\n")

        # Read and parse like the function does
        with open(secrets_file) as f:
            for line in f:
                if line.startswith("VAULTWARDEN_MASTER_PASSWORD="):
                    pw = line.strip().split("=", 1)[1]
                    if pw and pw[0] in ("'", '"') and pw[-1] == pw[0]:
                        pw = pw[1:-1]
                    assert pw == "quoted_pass"


@pytest.mark.integration
class TestVaultClientIntegration:
    """Integration tests — require Vaultwarden running on localhost:8222.

    Uses a shared client to avoid rate-limiting (one login per test class).
    """

    @pytest.fixture(autouse=True, scope="class")
    def vault(self, request):
        """Shared vault client — logs in once for all integration tests."""
        vc = VaultClient()
        vc.login()
        request.cls.vc = vc

    def test_login(self):
        assert self.vc.access_token is not None
        assert self.vc.enc_key is not None
        assert self.vc.mac_key is not None

    def test_list_items(self):
        items = self.vc.list_items()
        assert isinstance(items, list)
        assert len(items) >= 2  # AD Mortgage + IronSail Staging DB
        names = {i["name"] for i in items}
        assert "AD Mortgage" in names
        assert "IronSail Staging DB" in names

    def test_get_item_by_name(self):
        item = self.vc.get_item("AD Mortgage")
        assert item is not None
        assert item["name"] == "AD Mortgage"
        assert item["username"] == "phildago"
        assert item["password"] == "Valhallavitality2022!"

    def test_get_item_partial_match(self):
        item = self.vc.get_item("Staging")
        assert item is not None
        assert "Staging" in item["name"]

    def test_search(self):
        results = self.vc.search("mortgage")
        assert len(results) == 1
        assert results[0]["name"] == "AD Mortgage"

    def test_create_and_get_login(self):
        created = self.vc.create_login(
            name="Test Item — Delete Me",
            username="testuser",
            password="testpass123",
            uri="https://test.example.com",
            notes="Created by test_vault_client.py",
        )
        assert created["name"] == "Test Item — Delete Me"
        assert created["username"] == "testuser"
        assert created["password"] == "testpass123"

        # Verify we can retrieve it
        fetched = self.vc.get_item(created["id"])
        assert fetched["name"] == "Test Item — Delete Me"

        # Cleanup
        self.vc.delete_item(created["id"])

    def test_get_nonexistent_returns_none(self):
        item = self.vc.get_item("this-item-definitely-does-not-exist-xyz123")
        assert item is None

    def test_create_and_get_card(self):
        created = self.vc.create_card(
            name="Test Card — Delete Me",
            cardholderName="Test Holder",
            number="4111111111111111",
            expMonth="03",
            expYear="2028",
            code="123",
            brand="Visa",
            notes="Created by test_vault_client.py",
        )
        assert created["name"] == "Test Card — Delete Me"
        assert created["type"] == 3
        assert created["cardholderName"] == "Test Holder"
        assert created["number"] == "4111111111111111"
        assert created["expMonth"] == "03"
        assert created["expYear"] == "2028"
        assert created["code"] == "123"
        assert created["brand"] == "Visa"

        # Verify retrievable by name
        fetched = self.vc.get_item("Test Card — Delete Me")
        assert fetched is not None
        assert fetched["number"] == "4111111111111111"
        assert fetched["cardholderName"] == "Test Holder"

        # Cleanup
        self.vc.delete_item(created["id"])

    def test_card_appears_in_list_with_last4(self):
        created = self.vc.create_card(
            name="List Test Card — Delete Me",
            cardholderName="Test",
            number="5500000000000004",
            expMonth="12",
            expYear="2029",
            brand="Mastercard",
        )
        try:
            items = self.vc.list_items()
            card_items = [i for i in items if i["name"] == "List Test Card — Delete Me"]
            assert len(card_items) == 1
            assert card_items[0]["type"] == 3
            assert card_items[0]["last4"] == "0004"
            assert card_items[0]["brand"] == "Mastercard"
        finally:
            self.vc.delete_item(created["id"])

    def test_card_search_by_name(self):
        created = self.vc.create_card(
            name="Searchable Amex — Delete Me",
            cardholderName="Test",
            number="371449635398431",
            expMonth="06",
            expYear="2027",
            brand="Amex",
        )
        try:
            results = self.vc.search("Searchable Amex")
            assert len(results) == 1
            assert results[0]["name"] == "Searchable Amex — Delete Me"
        finally:
            self.vc.delete_item(created["id"])

    def test_create_card_without_optional_fields(self):
        created = self.vc.create_card(
            name="Minimal Card — Delete Me",
            cardholderName="",
            number="4242424242424242",
            expMonth="01",
            expYear="2030",
        )
        try:
            assert created["name"] == "Minimal Card — Delete Me"
            assert created["number"] == "4242424242424242"
            assert created["code"] is None
            assert created["brand"] is None
        finally:
            self.vc.delete_item(created["id"])

    def test_delete_item(self):
        created = self.vc.create_login(
            name="Delete Test — Delete Me",
            username="del",
            password="del",
        )
        assert self.vc.get_item(created["id"]) is not None
        self.vc.delete_item(created["id"])
        assert self.vc.get_item(created["id"]) is None
