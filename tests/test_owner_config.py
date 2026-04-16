"""Tests for robothor.owner_config — loader + OwnerConfig dataclass."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

import yaml

from robothor.constants import DEFAULT_TENANT
from robothor.owner_config import OwnerConfig, load_owner_config

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


class TestOwnerConfigDataclass:
    def test_full_name_combines_first_and_last(self):
        c = OwnerConfig(
            tenant_id="t", first_name="Alice", last_name="Example", email="a@example.com"
        )
        assert c.full_name == "Alice Example"

    def test_full_name_strips_when_last_missing(self):
        c = OwnerConfig(tenant_id="t", first_name="Madonna", last_name="", email="m@example.com")
        assert c.full_name == "Madonna"

    def test_matches_name_first_name_case_insensitive(self):
        c = OwnerConfig(
            tenant_id="t", first_name="Alice", last_name="Example", email="a@example.com"
        )
        assert c.matches_name("alice")
        assert c.matches_name("ALICE")
        assert c.matches_name("  Alice  ")

    def test_matches_name_full_name(self):
        c = OwnerConfig(
            tenant_id="t", first_name="Alice", last_name="Example", email="a@example.com"
        )
        assert c.matches_name("Alice Example")

    def test_matches_name_nickname(self):
        c = OwnerConfig(
            tenant_id="t",
            first_name="Alice",
            last_name="Example",
            email="a@example.com",
            nicknames=frozenset({"ali", "liss"}),
        )
        assert c.matches_name("ali")
        assert c.matches_name("LISS")
        assert not c.matches_name("bob")

    def test_matches_name_rejects_unrelated(self):
        c = OwnerConfig(
            tenant_id="t", first_name="Alice", last_name="Example", email="a@example.com"
        )
        assert not c.matches_name("Bob")
        assert not c.matches_name("")
        assert not c.matches_name("   ")

    def test_matches_name_empty_last_name_does_not_match_empty_input(self):
        c = OwnerConfig(tenant_id="t", first_name="Alice", last_name="", email="a@example.com")
        assert not c.matches_name("")

    def test_all_emails_deduplicates(self):
        c = OwnerConfig(
            tenant_id="t",
            first_name="Alice",
            last_name="Example",
            email="a@example.com",
            additional_emails=("a@example.com", "b@example.com", "  B@EXAMPLE.COM  "),
        )
        assert c.all_emails() == ("a@example.com", "b@example.com")


class TestYamlLoader:
    def test_minimal_valid_file(self, tmp_path):
        path = tmp_path / "owner.yaml"
        _write_yaml(path, {"first_name": "Alice", "last_name": "X", "email": "a@example.com"})
        cfg = load_owner_config(path=path)
        assert cfg is not None
        assert cfg.first_name == "Alice"
        assert cfg.last_name == "X"
        assert cfg.email == "a@example.com"
        assert cfg.tenant_id == DEFAULT_TENANT
        assert cfg.additional_emails == ()
        assert cfg.phone is None
        assert cfg.nicknames == frozenset()

    def test_full_file_with_all_fields(self, tmp_path):
        path = tmp_path / "owner.yaml"
        _write_yaml(
            path,
            {
                "tenant_id": "acme",
                "first_name": "Alice",
                "last_name": "Example",
                "email": "a@example.com",
                "additional_emails": ["alt@example.com", "other@example.com"],
                "phone": "+15550000000",
                "nicknames": ["ali", "liss"],
            },
        )
        cfg = load_owner_config(path=path)
        assert cfg is not None
        assert cfg.tenant_id == "acme"
        assert cfg.additional_emails == ("alt@example.com", "other@example.com")
        assert cfg.phone == "+15550000000"
        assert cfg.nicknames == frozenset({"ali", "liss"})

    def test_missing_file_returns_none_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
        monkeypatch.delenv("ROBOTHOR_OWNER_NAME", raising=False)
        assert load_owner_config(path=tmp_path / "does-not-exist.yaml") is None

    def test_missing_required_fields_returns_none(self, tmp_path):
        path = tmp_path / "owner.yaml"
        _write_yaml(path, {"first_name": "Alice"})  # no email
        assert load_owner_config(path=path) is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        path = tmp_path / "owner.yaml"
        path.write_text("{{{ not valid yaml", encoding="utf-8")
        assert load_owner_config(path=path) is None

    def test_yaml_not_mapping_returns_none(self, tmp_path):
        path = tmp_path / "owner.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert load_owner_config(path=path) is None

    def test_email_normalized_to_lowercase(self, tmp_path):
        path = tmp_path / "owner.yaml"
        _write_yaml(path, {"first_name": "Alice", "last_name": "X", "email": "A@EXAMPLE.COM"})
        cfg = load_owner_config(path=path)
        assert cfg is not None
        assert cfg.email == "a@example.com"

    def test_nickname_as_single_string_coerces_to_set(self, tmp_path):
        path = tmp_path / "owner.yaml"
        _write_yaml(
            path,
            {
                "first_name": "Alice",
                "last_name": "X",
                "email": "a@example.com",
                "nicknames": "ali",
            },
        )
        cfg = load_owner_config(path=path)
        assert cfg is not None
        assert cfg.nicknames == frozenset({"ali"})


class TestEnvFallback:
    def test_env_fallback_when_yaml_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "legacy@example.com")
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Legacy User")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = load_owner_config(path=tmp_path / "missing.yaml")
        assert cfg is not None
        assert cfg.email == "legacy@example.com"
        assert cfg.first_name == "Legacy"
        assert cfg.last_name == "User"
        assert cfg.tenant_id == DEFAULT_TENANT
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)

    def test_env_fallback_single_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "solo@example.com")
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Madonna")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = load_owner_config(path=tmp_path / "missing.yaml")
        assert cfg is not None
        assert cfg.first_name == "Madonna"
        assert cfg.last_name == ""

    def test_env_fallback_requires_both_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "legacy@example.com")
        monkeypatch.delenv("ROBOTHOR_OWNER_NAME", raising=False)
        assert load_owner_config(path=tmp_path / "missing.yaml") is None

    def test_yaml_takes_priority_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "env@example.com")
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Env User")
        path = tmp_path / "owner.yaml"
        _write_yaml(
            path, {"first_name": "YamlFirst", "last_name": "YamlLast", "email": "yaml@example.com"}
        )
        cfg = load_owner_config(path=path)
        assert cfg is not None
        assert cfg.email == "yaml@example.com"
        assert cfg.first_name == "YamlFirst"
