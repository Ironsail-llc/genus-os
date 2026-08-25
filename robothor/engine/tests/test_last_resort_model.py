"""Every agent gets the instance's model of last resort, automatically.

2026-08-25, prompted by the operator's drill question — "imagine OpenRouter
goes down for a day": the offline tier existed, the model was pulled, Ollama
was upgraded and enabled — and exactly ONE of 23 agents would ever reach it,
because `_defaults.yaml` fallback lists are REPLACED (not merged) by each
manifest's own `model:` block, a semantics trap this project has already
documented once. The other 22 agents' chains ended at cloud providers.

The fix is an engine-level guarantee rather than 22 manifest edits: when
``ROBOTHOR_LAST_RESORT_MODEL`` is set, every loaded agent config gets it
appended to its fallback chain. One knob per instance, every current and
FUTURE agent covered, no manifest churn, and unset means byte-identical
behavior.
"""

from __future__ import annotations

import textwrap

from robothor.engine.config import load_agent_config


def _write_manifest(tmp_path, fallbacks="") -> str:
    (tmp_path / "probe.yaml").write_text(
        textwrap.dedent(
            f"""\
            id: probe
            name: Probe
            model:
              primary: openrouter/test/primary
              {fallbacks}
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


class TestLastResortModel:
    def test_appended_to_every_chain(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local:27b")
        d = _write_manifest(
            tmp_path,
            "fallbacks:\n                - openrouter/test/fallback",
        )
        config = load_agent_config("probe", d)
        assert config.model_fallbacks[-1] == "ollama_chat/local:27b"
        assert config.model_fallbacks[0] == "openrouter/test/fallback"

    def test_appended_even_with_no_declared_fallbacks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local:27b")
        config = load_agent_config("probe", _write_manifest(tmp_path))
        assert config.model_fallbacks == ["ollama_chat/local:27b"]

    def test_not_duplicated_when_already_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local:27b")
        d = _write_manifest(
            tmp_path,
            "fallbacks:\n                - ollama_chat/local:27b",
        )
        config = load_agent_config("probe", d)
        assert config.model_fallbacks.count("ollama_chat/local:27b") == 1

    def test_not_appended_when_it_is_the_primary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "openrouter/test/primary")
        config = load_agent_config("probe", _write_manifest(tmp_path))
        assert config.model_fallbacks == []

    def test_unset_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_LAST_RESORT_MODEL", raising=False)
        d = _write_manifest(
            tmp_path,
            "fallbacks:\n                - openrouter/test/fallback",
        )
        config = load_agent_config("probe", d)
        assert config.model_fallbacks == ["openrouter/test/fallback"]
