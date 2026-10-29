"""``llm_client.fleet_model_chain`` — the model chain a plugin dials.

A plugin tool must never pin a model: the operator chooses the fleet's models
in ``_defaults.yaml`` and a pinned name goes stale the day they change it. So
the engine hands plugins the fleet's own chain, primary first, read from the
same defaults file the engine loads. When there is no such file the chain is
whatever the engine itself would still dial — the instance's model of last
resort — and empty only when there is nothing at all.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _write_defaults(directory, body: str) -> None:
    (directory / "_defaults.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def no_last_resort():
    with patch("robothor.engine.config.last_resort_model", return_value=""):
        yield


def test_primary_then_fallbacks_in_order(tmp_path, no_last_resort):
    from robothor.engine.llm_client import fleet_model_chain

    _write_defaults(
        tmp_path,
        "model:\n"
        "  primary: openrouter/example/alpha\n"
        "  fallbacks:\n"
        "    - openrouter/example/beta\n"
        "    - openrouter/example/gamma\n",
    )

    assert fleet_model_chain(tmp_path) == [
        "openrouter/example/alpha",
        "openrouter/example/beta",
        "openrouter/example/gamma",
    ]


def test_with_no_argument_it_reads_the_engine_manifest_dir(tmp_path, monkeypatch, no_last_resort):
    from robothor.engine.llm_client import fleet_model_chain

    _write_defaults(tmp_path, "model:\n  primary: openrouter/example/alpha\n")
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path))

    assert fleet_model_chain() == ["openrouter/example/alpha"]


def test_the_last_resort_model_ends_the_chain(tmp_path):
    from robothor.engine.llm_client import fleet_model_chain

    _write_defaults(tmp_path, "model:\n  primary: openrouter/example/alpha\n")
    with patch("robothor.engine.config.last_resort_model", return_value="ollama_chat/example:8b"):
        assert fleet_model_chain(tmp_path) == [
            "openrouter/example/alpha",
            "ollama_chat/example:8b",
        ]


def test_without_a_defaults_file_it_falls_back_to_the_engine_default(tmp_path):
    from robothor.engine.llm_client import fleet_model_chain

    with patch("robothor.engine.config.last_resort_model", return_value="ollama_chat/example:8b"):
        assert fleet_model_chain(tmp_path) == ["ollama_chat/example:8b"]


def test_without_a_defaults_file_or_a_last_resort_the_chain_is_empty(tmp_path, no_last_resort):
    from robothor.engine.llm_client import fleet_model_chain

    assert fleet_model_chain(tmp_path) == []
