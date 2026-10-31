"""The per-tenant engine URL settings are declared before anything reads them.

A child tenant's engine is reached at a URL the bridge and dashboard derive
per tenant. The Helm chart sets the template on pods, and
``test_every_env_read_is_declared`` scans Helm templates too, so both names
must exist in the registry on the branch the chart builds on.
"""

from __future__ import annotations

import pytest

NAMES = {
    "GENUS_TENANT_ENGINE_URLS": "ROBOTHOR_TENANT_ENGINE_URLS",
    "GENUS_TENANT_ENGINE_URL_TEMPLATE": "ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE",
}


@pytest.mark.parametrize("name", sorted(NAMES))
def test_the_setting_is_declared(name: str) -> None:
    from robothor.settings.registry import declared_env_names

    assert name in declared_env_names()


@pytest.mark.parametrize(("name", "alias"), sorted(NAMES.items()))
def test_the_robothor_alias_is_declared_and_deprecated(name: str, alias: str) -> None:
    from robothor.settings.aliases import DEPRECATED_ALIASES
    from robothor.settings.registry import declared_env_names

    assert alias in declared_env_names()
    assert DEPRECATED_ALIASES.get(alias) == name


def test_both_default_to_empty() -> None:
    from robothor.settings.model import AuthSettings

    fields = AuthSettings.model_fields
    assert fields["tenant_engine_urls"].default == ""
    assert fields["tenant_engine_url_template"].default == ""
