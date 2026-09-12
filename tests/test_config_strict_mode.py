"""What an unknown key in ``config.yaml`` does, and who decides.

``extra="forbid"`` on every settings group means a typo reaches validation and
is rejected by name -- the difference between a typo that is reported and a
setting that silently never applied. But an existing install upgrading into
that rule would fail to start on a key that has been sitting in its config file
harmlessly for months, so ``ROBOTHOR_CONFIG_STRICT_MODE`` decides which it is:

``off``       drop the key, say nothing.
``observe``   drop the key, log one warning naming it. The default.
``enforce``   refuse to resolve settings at all, naming the key.
"""

from __future__ import annotations

import logging
import os

import pytest


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A workspace of our own, with none of the box's own configuration."""
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES, reset_alias_warnings
    from robothor.settings.sources import reset_unknown_key_warnings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_alias_warnings()
    reset_unknown_key_warnings()
    reset_settings()
    yield
    reset_settings()
    reset_alias_warnings()
    reset_unknown_key_warnings()


def _write_config(workspace, body: str) -> None:
    (workspace / ".robothor").mkdir(parents=True, exist_ok=True)
    (workspace / ".robothor" / "config.yaml").write_text(body, encoding="utf-8")


UNKNOWN_FIELD = "settings:\n  engine:\n    max_concurrent_agents: 7\n    nto_a_real_field: 1\n"
UNKNOWN_GROUP = (
    "settings:\n  engine:\n    max_concurrent_agents: 7\n  nto_a_real_group:\n    a: 1\n"
)


def test_enforce_refuses_and_names_the_key(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_CONFIG_STRICT_MODE", "enforce")
    _write_config(tmp_path, UNKNOWN_FIELD)

    with pytest.raises(Exception) as excinfo:
        get_settings()
    assert "nto_a_real_field" in str(excinfo.value)


def test_enforce_refuses_an_unknown_group(tmp_path, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_CONFIG_STRICT_MODE", "enforce")
    _write_config(tmp_path, UNKNOWN_GROUP)

    with pytest.raises(Exception) as excinfo:
        get_settings()
    assert "nto_a_real_group" in str(excinfo.value)


def test_observe_is_the_default_and_warns_once(tmp_path, caplog) -> None:
    """An existing install starts, keeps its known settings, and is told."""
    from robothor.settings import get_settings, reset_settings

    _write_config(tmp_path, UNKNOWN_FIELD)

    with caplog.at_level(logging.WARNING, logger="robothor.settings.sources"):
        assert get_settings().engine.max_concurrent_agents == 7
        reset_settings()
        assert get_settings().engine.max_concurrent_agents == 7

    warnings = [r for r in caplog.records if "nto_a_real_field" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "config.yaml" in warnings[0].getMessage()


def test_off_is_silent(tmp_path, caplog, monkeypatch) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_CONFIG_STRICT_MODE", "off")
    _write_config(tmp_path, UNKNOWN_FIELD)

    with caplog.at_level(logging.WARNING, logger="robothor.settings.sources"):
        assert get_settings().engine.max_concurrent_agents == 7
    assert not [r for r in caplog.records if "nto_a_real_field" in r.getMessage()]


def test_mode_can_be_set_in_the_file_it_governs(tmp_path, caplog) -> None:
    """No chicken-and-egg: config.yaml may set its own strictness."""
    from robothor.settings import get_settings

    _write_config(
        tmp_path,
        "settings:\n  flags:\n    config_strict_mode: enforce\n  engine:\n    nope: 1\n",
    )
    with pytest.raises(Exception) as excinfo:
        get_settings()
    assert "nope" in str(excinfo.value)


def test_env_beats_the_file_for_the_mode(tmp_path, monkeypatch, caplog) -> None:
    from robothor.settings import get_settings

    monkeypatch.setenv("ROBOTHOR_CONFIG_STRICT_MODE", "off")
    _write_config(
        tmp_path,
        "settings:\n  flags:\n    config_strict_mode: enforce\n  engine:\n    nope: 1\n",
    )
    with caplog.at_level(logging.WARNING, logger="robothor.settings.sources"):
        # The unknown key is dropped in silence rather than refused, and the
        # effective value of the field is the environment's, as for any other.
        assert get_settings().flags.config_strict_mode == "off"
    assert not [r for r in caplog.records if "nope" in r.getMessage()]


def test_a_key_written_under_its_env_name_is_not_unknown(tmp_path, caplog) -> None:
    """``populate_by_name`` means either spelling is legal; neither may warn."""
    from robothor.settings import get_settings

    _write_config(tmp_path, "settings:\n  engine:\n    ROBOTHOR_MAX_CONCURRENT_AGENTS: 7\n")
    with caplog.at_level(logging.WARNING, logger="robothor.settings.sources"):
        assert get_settings().engine.max_concurrent_agents == 7
    assert not caplog.records


def test_unknown_keys_are_reported_for_the_cli(tmp_path) -> None:
    """``genus config validate`` needs the list, not just a log line."""
    from robothor.settings.sources import unknown_config_keys

    _write_config(tmp_path, UNKNOWN_FIELD + "  nto_a_real_group:\n    a: 1\n")
    assert unknown_config_keys() == ["engine.nto_a_real_field", "nto_a_real_group"]


def test_no_config_file_means_no_unknown_keys(tmp_path) -> None:
    from robothor.settings.sources import unknown_config_keys

    assert unknown_config_keys() == []
