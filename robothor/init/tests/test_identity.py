"""Who the instance belongs to, written once, in one place.

``genus init`` used to put ``ROBOTHOR_OWNER_NAME``/``ROBOTHOR_OWNER_EMAIL``
into the workspace ``.env`` and never write ``owner.yaml`` at all, so the file
the platform actually reads did not exist and the identity lived somewhere
nothing looked. These tests pin the correction: owner.yaml is the identity,
the operator account is seeded in the SAME tenant, and ``.env`` holds neither.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from robothor.init.identity import Identity, bootstrap_operator, write_identity
from robothor.init.steps import StepError


def _identity(**kwargs: Any) -> Identity:
    base = {"name": "Alice Example", "email": "alice@example.com", "tenant_id": "default"}
    base.update(kwargs)
    return Identity(**base)  # type: ignore[arg-type]


class TestOwnerYaml:
    def test_it_writes_the_chosen_tenant(self, tmp_path):
        path = tmp_path / ".robothor" / "owner.yaml"

        assert write_identity(_identity(tenant_id="acme"), path=path) is True

        document = yaml.safe_load(path.read_text())
        assert document["tenant_id"] == "acme"
        assert document["email"] == "alice@example.com"
        assert document["first_name"] == "Alice"

    def test_an_existing_identity_is_never_overwritten(self, tmp_path):
        path = tmp_path / ".robothor" / "owner.yaml"
        write_identity(_identity(), path=path)

        assert (
            write_identity(_identity(name="Bob Example", email="bob@example.com"), path=path)
            is False
        )
        assert yaml.safe_load(path.read_text())["email"] == "alice@example.com"

    def test_an_incomplete_identity_writes_nothing(self, tmp_path):
        path = tmp_path / ".robothor" / "owner.yaml"

        assert write_identity(_identity(email=""), path=path) is False
        assert not path.exists()

    def test_the_file_is_not_world_readable(self, tmp_path):
        path = tmp_path / ".robothor" / "owner.yaml"
        write_identity(_identity(), path=path)

        assert path.stat().st_mode & 0o077 == 0


class TestTheWizardWritesWhereTheLoaderReads:
    def test_the_owner_config_override_is_honoured(self, tmp_path, monkeypatch):
        """Writing the hardcoded path while the loader reads an override is a
        wizard that reports an identity the platform cannot find."""
        from robothor.init.context import InitContext
        from robothor.init.steps import IdentityStep

        override = tmp_path / "elsewhere" / "owner.yaml"
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(override))
        ctx = InitContext(workspace=tmp_path / "workspace")

        assert IdentityStep._path(ctx) == override

    def test_without_an_override_it_is_the_hardcoded_path(self, tmp_path, monkeypatch):
        from robothor.constants import owner_config_path
        from robothor.init.context import InitContext
        from robothor.init.steps import IdentityStep

        monkeypatch.delenv("ROBOTHOR_OWNER_CONFIG", raising=False)
        ctx = InitContext(workspace=tmp_path / "workspace")

        assert IdentityStep._path(ctx) == owner_config_path()


class TestEnvFileNoLongerCarriesTheOperator:
    def test_the_env_writer_emits_no_owner_variables(self, tmp_path):
        from robothor.config import DatabaseConfig, OllamaConfig, RedisConfig
        from robothor.setup import write_env_file

        path = tmp_path / ".env"
        write_env_file(
            path,
            DatabaseConfig(host="127.0.0.1", port=5432, name="db", user="u", password=""),
            RedisConfig(host="127.0.0.1", port=6379, db=0, password=""),
            OllamaConfig(host="127.0.0.1", port=11434),
            yes=True,
        )

        content = path.read_text()
        assert "ROBOTHOR_OWNER_NAME" not in content
        assert "ROBOTHOR_OWNER_EMAIL" not in content


class TestOperatorAccount:
    def test_the_account_is_created_in_the_owner_yaml_tenant(self):
        seen: list[str] = []

        def fake_bootstrap() -> dict[str, Any]:
            seen.append("called")
            return {"id": "user-1", "tenant_id": "acme", "email": "alice@example.com"}

        account = bootstrap_operator(_identity(tenant_id="acme"), bootstrap=fake_bootstrap)

        assert seen == ["called"]
        assert account is not None
        assert account["tenant_id"] == "acme"

    def test_a_tenant_disagreement_is_a_step_failure_not_a_silent_pass(self):
        def fake_bootstrap() -> dict[str, Any]:
            return {"id": "user-1", "tenant_id": "somewhere-else", "email": "alice@example.com"}

        with pytest.raises(StepError) as exc:
            bootstrap_operator(_identity(tenant_id="acme"), bootstrap=fake_bootstrap)

        assert "acme" in str(exc.value)
        assert "somewhere-else" in str(exc.value)

    def test_no_operator_configured_is_a_step_failure(self):
        with pytest.raises(StepError):
            bootstrap_operator(_identity(), bootstrap=lambda: None)

    def test_no_password_is_set_here(self):
        """The browser wizard sets the password; init only seeds the account."""
        captured: dict[str, Any] = {}

        def fake_bootstrap(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"id": "user-1", "tenant_id": "default", "email": "alice@example.com"}

        bootstrap_operator(_identity(), bootstrap=fake_bootstrap)

        assert captured == {}
