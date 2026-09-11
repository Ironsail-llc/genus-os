"""The settings registry is the declaration of every platform config name.

An enterprise operator cannot configure what is not declared. Before this
package, 137 ``ROBOTHOR_*`` variables were read across the platform, 91 of them
documented nowhere. These tests make that state unreachable:

* ``test_every_env_read_is_declared`` — the scan in ``scripts/list_env_reads.py``
  finds every name the shipped platform reads; each must be a declared field or
  a declared alias. Adding an undeclared reader fails here.
* ``test_env_read_sites_outside_settings_do_not_grow`` — a one-way ratchet on
  the number of raw ``os.environ`` readers, so later PRs can only move readers
  behind ``get_settings()``.
* the rest pin the precedence, the strictness, and the import cost the CLI
  depends on.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_SCRIPT = REPO_ROOT / "scripts" / "list_env_reads.py"

#: Python env-read call sites outside ``robothor/settings/``, measured on
#: 2026-09-11 by ``python scripts/list_env_reads.py --count-sites``.
#:
#: This is a RATCHET: it may only ever be lowered. Lower it by moving readers
#: behind ``robothor.settings.get_settings()`` and re-running that command --
#: never by raising the number to accommodate a new ``os.environ`` call.
ENV_READ_SITE_BASELINE = 493


def _discovery():
    spec = importlib.util.spec_from_file_location("list_env_reads", DISCOVERY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Every test sees a settings object built from its own environment."""
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


def test_every_env_read_is_declared() -> None:
    from robothor.settings.registry import declared_env_names

    found = set(_discovery().discover(REPO_ROOT))
    undeclared = sorted(found - declared_env_names())
    assert not undeclared, (
        "these configuration names are read by the platform but declared nowhere in "
        "robothor/settings/model.py:\n  " + "\n  ".join(undeclared)
    )


def test_env_read_sites_outside_settings_do_not_grow() -> None:
    count = _discovery().count_env_read_sites(REPO_ROOT)
    assert count <= ENV_READ_SITE_BASELINE, (
        f"{count} raw env-read call sites outside robothor/settings/, baseline is "
        f"{ENV_READ_SITE_BASELINE}. Read configuration through "
        "robothor.settings.get_settings() instead of os.environ."
    )


def test_declared_names_are_documented() -> None:
    from robothor.settings.registry import field_index

    index = field_index()
    assert index, "the registry declared no fields at all"
    for env_name, info in index.items():
        assert info["description"].strip(), f"{env_name} has an empty description"
        assert info["env"] == env_name or env_name in info["aliases"], (
            f"{env_name} is indexed under a name that is neither its primary env "
            f"name ({info['env']!r}) nor one of its aliases ({info['aliases']!r})"
        )
    primaries = {info["env"] for info in index.values()}
    for primary in primaries:
        assert index[primary]["env"] == primary


def test_declared_names_carry_full_metadata() -> None:
    from robothor.settings.registry import field_index

    for env_name, info in field_index().items():
        for key in ("restart_required", "secret", "governed"):
            assert isinstance(info[key], bool), f"{env_name}.{key} is not a bool"
        assert info["since"], f"{env_name} declares no 'since'"
        assert info["group"], f"{env_name} belongs to no group"


def test_no_secret_has_a_default_value() -> None:
    """A default for a secret is a credential committed to git."""
    from robothor.settings.registry import field_index

    for env_name, info in field_index().items():
        if info["secret"]:
            assert info["default"] in ("", None), (
                f"{env_name} is marked secret but ships a default value"
            )


def _write_config(workspace: Path, body: str) -> None:
    (workspace / ".robothor").mkdir(parents=True, exist_ok=True)
    (workspace / ".robothor" / "config.yaml").write_text(body, encoding="utf-8")


def test_config_yaml_precedence(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings, reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", raising=False)
    _write_config(
        tmp_path,
        "settings:\n  engine:\n    max_concurrent_agents: 7\n",
    )

    # config.yaml beats the built-in default...
    assert get_settings().engine.max_concurrent_agents == 7

    # ...and the environment beats config.yaml.
    reset_settings()
    monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", "9")
    assert get_settings().engine.max_concurrent_agents == 9

    # ...and an explicit runtime override beats everything.
    assert get_settings(engine={"max_concurrent_agents": 11}).engine.max_concurrent_agents == 11


def test_runtime_override_is_not_cached(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", "4")
    assert get_settings(engine={"max_concurrent_agents": 12}).engine.max_concurrent_agents == 12
    assert get_settings().engine.max_concurrent_agents == 4


def test_unknown_key_is_rejected(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    _write_config(tmp_path, "settings:\n  not_a_real_group: 1\n")

    with pytest.raises(Exception) as excinfo:
        get_settings()
    assert "not_a_real_group" in str(excinfo.value)


def test_unknown_key_inside_a_group_is_rejected(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    _write_config(tmp_path, "settings:\n  engine:\n    not_a_real_field: 1\n")

    with pytest.raises(Exception) as excinfo:
        get_settings()
    assert "not_a_real_field" in str(excinfo.value)


def test_missing_config_yaml_is_not_an_error(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    assert get_settings().engine.max_concurrent_agents >= 1


def test_deprecated_alias_warns_once_naming_the_replacement(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings, reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES, reset_alias_warnings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    reset_alias_warnings()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings = get_settings()
        reset_settings()
        get_settings()

    assert settings.channels.telegram_chat_id == "12345"
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    telegram = [m for m in messages if "TELEGRAM_CHAT_ID" in m]
    assert len(telegram) == 1, f"expected exactly one warning, got {telegram}"
    assert DEPRECATED_ALIASES["TELEGRAM_CHAT_ID"] in telegram[0]


def test_deprecated_primary_name_also_warns(tmp_path, monkeypatch) -> None:
    """A setting can be deprecated without having a replacement env name.

    The operator identity variables are the case: ``ROBOTHOR_OWNER_EMAIL`` is
    still the only environment name the field has, but ~/.robothor/owner.yaml
    replaced it outright. Reading it must warn even though it arrived under
    the field's primary name rather than an alias.
    """
    from robothor.settings import get_settings
    from robothor.settings.aliases import reset_alias_warnings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "owner@example.com")
    reset_alias_warnings()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings = get_settings()

    assert settings.channels.owner_email == "owner@example.com"
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    owner = [m for m in messages if "ROBOTHOR_OWNER_EMAIL" in m]
    assert len(owner) == 1, f"expected exactly one warning, got {owner}"
    assert "owner.yaml" in owner[0]


def test_deprecated_aliases_point_at_something_declared() -> None:
    from robothor.settings.aliases import DEPRECATED_ALIASES
    from robothor.settings.registry import declared_env_names

    declared = declared_env_names()
    for old, new in DEPRECATED_ALIASES.items():
        assert old in declared, f"deprecated alias {old} is not declared on any field"
        # The replacement is either another env name or a file that replaced it.
        assert new in declared or "/" in new or new.endswith(".yaml"), (
            f"{old} claims to be replaced by {new!r}, which is neither a declared "
            "env name nor a path"
        )


def test_cli_import_does_not_load_pydantic_settings() -> None:
    """``genus --help`` must not pay for the settings model.

    pydantic-settings pulls pydantic's full machinery; importing it on every
    CLI invocation is a startup cost no command that never reads settings
    should pay. ``get_settings()`` imports it lazily.
    """
    code = "import sys; import robothor.cli; print('pydantic_settings' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False", (
        "importing robothor.cli loaded pydantic_settings; keep the import inside get_settings()"
    )


def test_settings_package_import_does_not_load_pydantic_settings() -> None:
    code = "import sys; import robothor.settings; print('pydantic_settings' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_config_schema_command_prints_json(capsys) -> None:
    import argparse
    import json

    from robothor.cli.admin import cmd_config

    assert cmd_config(argparse.Namespace(config_command="schema")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "GenusSettings"
    assert "engine" in payload["properties"]
