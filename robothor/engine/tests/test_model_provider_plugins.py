"""A model the registry has never heard of should be addable from outside.

`get_model_limits` consults a hardcoded `_MODEL_REGISTRY`, then litellm's
bundled catalog, then a conservative 128K fallback. An instance running a
model neither table knows gets the fallback and a log line telling it to
edit core:

    Unknown model 'openrouter/z-ai/glm-5.2' — not in the static registry or
    litellm catalog; using conservative 128K fallback. Add it to
    model_registry._MODEL_REGISTRY.

That message appeared 655 times in one benchmark run on this box. "Add it
to core" is not an answer for an instance that isn't the platform, and it
is the axis DeepSeek Harness leads on — model adapters are plugins there
and a hardcoded table here.

Precedence is the whole design. Curated entries carry Genus-specific facts
(codex priced at zero, hand-pinned cache flags) and must keep winning, so a
plugin extends coverage without being able to overwrite a deliberate pin:

    curated registry  >  plugin  >  litellm catalog  >  fallback
"""

from __future__ import annotations

import pytest

from robothor.engine.model_registry import ModelLimits, get_model_limits
from robothor.plugins import reload_plugins


def _limits(max_input: int = 200_000) -> ModelLimits:
    return ModelLimits(
        max_input_tokens=max_input,
        max_output_tokens=8_192,
        default_output_tokens=4_096,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
    )


class _ModelEP:
    group = "genus.models"

    def __init__(self, name: str, models: dict):
        self.name = name
        self._models = models

    def load(self):
        return {"genus_contract_version": "1.0", "models": self._models}


@pytest.fixture
def install(monkeypatch):
    """Install plugin-contributed models for the duration of one test."""

    def _install(models: dict, name: str = "testmodels"):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_ModelEP(name, models)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestAPluginCanRegisterAModel:
    def test_an_unknown_model_falls_back_before_the_plugin_exists(self):
        limits = get_model_limits("acme/unheard-of-v1")
        assert limits.max_input_tokens == 128_000, "expected the conservative fallback"

    def test_the_plugin_supplies_real_limits(self, install):
        install({"acme/unheard-of-v1": _limits(400_000)})
        assert get_model_limits("acme/unheard-of-v1").max_input_tokens == 400_000

    def test_a_dict_payload_is_accepted_too(self, install):
        """Plugin authors should not need to import our dataclass."""
        install(
            {
                "acme/dict-model": {
                    "max_input_tokens": 300_000,
                    "max_output_tokens": 4096,
                    "default_output_tokens": 2048,
                    "input_cost_per_token": 0.0,
                    "output_cost_per_token": 0.0,
                }
            }
        )
        assert get_model_limits("acme/dict-model").max_input_tokens == 300_000

    def test_removing_the_plugin_withdraws_the_model(self, install, monkeypatch):
        install({"acme/temporary": _limits(500_000)})
        assert get_model_limits("acme/temporary").max_input_tokens == 500_000

        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        assert get_model_limits("acme/temporary").max_input_tokens == 128_000


class TestCuratedEntriesStillWin:
    def test_a_plugin_cannot_overwrite_a_curated_model(self, install):
        """Curated entries carry facts a plugin must not be able to rewrite.

        codex is priced at zero deliberately, because usage is governed by
        plan quota rather than API tokens. A package that could overwrite
        that would silently change this instance's cost accounting.
        """
        real = get_model_limits("codex/gpt-5.5")
        install({"codex/gpt-5.5": _limits(1)})
        after = get_model_limits("codex/gpt-5.5")
        assert after.max_input_tokens == real.max_input_tokens
        assert after.input_cost_per_token == 0.0

    def test_the_loader_refuses_a_reserved_model_name(self):
        """Refusal happens at the loader, and is reported rather than silent."""
        from robothor.plugins import load_plugins
        from robothor.plugins.loader import _GROUPS  # noqa: F401

        ep = _ModelEP("greedy", {"codex/gpt-5.5": _limits(1)})
        loaded = load_plugins(entry_points=[ep], reserved_names={"codex/gpt-5.5"})
        assert "codex/gpt-5.5" not in (loaded.models or {})
        assert any("codex/gpt-5.5" in f.reason for f in loaded.failures), (
            f"the refusal was silent: {loaded.failures}"
        )


class TestBadPayloadsAreRefusedNotCrashed:
    def test_a_malformed_entry_does_not_break_lookups(self, install):
        install({"acme/broken": "not-a-model", "acme/fine": _limits(250_000)})
        assert get_model_limits("acme/fine").max_input_tokens == 250_000
        assert get_model_limits("acme/broken").max_input_tokens == 128_000

    def test_a_plugin_that_raises_on_load_is_survivable(self, monkeypatch):
        from robothor.plugins import loader

        class _Boom:
            name = "boom"
            group = "genus.models"

            def load(self):
                raise RuntimeError("bad package")

        monkeypatch.setattr(loader, "_discover", lambda: [_Boom()])
        reload_plugins()
        assert get_model_limits("codex/gpt-5.5") is not None


class TestTheGroupIsWiredEndToEnd:
    def test_the_loader_declares_the_group(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.models") == "models"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"acme/visible": _limits()})
        assert "acme/visible" in (load_plugins(reserved_names=set()).models or {})
