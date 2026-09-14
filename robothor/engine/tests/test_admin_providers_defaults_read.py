"""``GET /api/admin/defaults``: the fleet default model block, readable.

The Providers page writes the block (``PATCH /api/providers/defaults``) but had
nothing to read it from, so the first version shipped with the default-model
field blank (B6 review, 2026-09-14). Manifests stay the source of truth: this
reads ``_defaults.yaml`` and nothing else.
"""

from __future__ import annotations

import yaml
from starlette.testclient import TestClient

from robothor.engine import admin_providers
from robothor.engine.tests.test_admin_providers import _make_app


def _client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


def test_it_returns_the_primary_and_fallbacks_from_the_defaults_file(tmp_path, monkeypatch) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "_defaults.yaml").write_text(
        yaml.safe_dump({"model": {"primary": "openrouter/z-ai/glm-5", "fallbacks": ["x/y"]}})
    )
    monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)
    from robothor.engine import config as engine_config

    engine_config.reset_defaults_cache()
    body = _client().get("/api/admin/defaults").json()
    assert body == {"primary": "openrouter/z-ai/glm-5", "fallbacks": ["x/y"]}


def test_a_missing_defaults_file_is_an_instance_without_defaults_not_an_error(
    tmp_path, monkeypatch
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)
    from robothor.engine import config as engine_config

    engine_config.reset_defaults_cache()
    response = _client().get("/api/admin/defaults")
    assert response.status_code == 200
    assert response.json() == {"primary": None, "fallbacks": []}


def test_reading_does_not_change_the_file(tmp_path, monkeypatch) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    path = agents / "_defaults.yaml"
    path.write_text(yaml.safe_dump({"model": {"primary": "a/b", "temperature": 0.2}, "other": 1}))
    before = path.read_text()
    monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)
    from robothor.engine import config as engine_config

    engine_config.reset_defaults_cache()
    _client().get("/api/admin/defaults")
    assert path.read_text() == before
