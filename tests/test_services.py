"""Tests for robothor.services.registry — service discovery."""

import json

import pytest

from robothor.config import reset_config
from robothor.services.registry import (
    _reset_cache,
    get_health_url,
    get_service,
    get_service_url,
    list_services,
    topological_sort,
)


@pytest.fixture(autouse=True)
def clean_state():
    reset_config()
    _reset_cache()
    yield
    _reset_cache()
    reset_config()


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    """Create a temporary service manifest."""
    manifest = {
        "services": {
            "bridge": {
                "port": 9100,
                "host": "127.0.0.1",
                "protocol": "http",
                "health": "/health",
                "systemd_unit": "robothor-bridge.service",
                "dependencies": ["postgresql", "redis"],
            },
            "postgresql": {
                "port": 5432,
                "host": "127.0.0.1",
                "protocol": "tcp",
                "health": None,
                "dependencies": [],
            },
            "redis": {
                "port": 6379,
                "host": "127.0.0.1",
                "protocol": "tcp",
                "health": None,
                "dependencies": [],
            },
        }
    }
    path = tmp_path / "robothor-services.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("ROBOTHOR_SERVICES_MANIFEST", str(path))
    return path


class TestServiceLookup:
    def test_get_service(self, manifest_file):
        svc = get_service("bridge")
        assert svc is not None
        assert svc["port"] == 9100

    def test_get_service_unknown(self, manifest_file):
        assert get_service("nonexistent") is None

    def test_get_service_url(self, manifest_file):
        url = get_service_url("bridge")
        assert url == "http://127.0.0.1:9100"

    def test_get_service_url_with_path(self, manifest_file):
        url = get_service_url("bridge", "/api/people")
        assert url == "http://127.0.0.1:9100/api/people"

    def test_get_service_url_unknown(self, manifest_file):
        assert get_service_url("nonexistent") is None

    def test_env_override(self, manifest_file, monkeypatch):
        monkeypatch.setenv("BRIDGE_URL", "http://remote:9200")
        url = get_service_url("bridge")
        assert url == "http://remote:9200"

    def test_env_override_with_path(self, manifest_file, monkeypatch):
        monkeypatch.setenv("BRIDGE_URL", "http://remote:9200")
        url = get_service_url("bridge", "/api/people")
        assert url == "http://remote:9200/api/people"


class TestHealthUrl:
    def test_health_url(self, manifest_file):
        url = get_health_url("bridge")
        assert url == "http://127.0.0.1:9100/health"

    def test_no_health(self, manifest_file):
        assert get_health_url("postgresql") is None

    def test_unknown_service(self, manifest_file):
        assert get_health_url("nonexistent") is None


class TestListServices:
    def test_list(self, manifest_file):
        services = list_services()
        assert "bridge" in services
        assert "postgresql" in services
        assert len(services) == 3


class TestTopologicalSort:
    def test_sort_order(self, manifest_file):
        order = topological_sort()
        # postgresql and redis should come before bridge
        bridge_idx = order.index("bridge")
        pg_idx = order.index("postgresql")
        redis_idx = order.index("redis")
        assert pg_idx < bridge_idx
        assert redis_idx < bridge_idx

    def test_cycle_detection(self, tmp_path, monkeypatch):
        manifest = {
            "services": {
                "a": {"port": 1, "dependencies": ["b"]},
                "b": {"port": 2, "dependencies": ["a"]},
            }
        }
        path = tmp_path / "robothor-services.json"
        path.write_text(json.dumps(manifest))
        monkeypatch.setenv("ROBOTHOR_SERVICES_MANIFEST", str(path))
        _reset_cache()
        with pytest.raises(ValueError, match="Circular dependency"):
            topological_sort()


class TestNoManifest:
    def test_graceful_without_manifest(self, tmp_path, monkeypatch):
        _reset_cache()
        monkeypatch.setenv("ROBOTHOR_SERVICES_MANIFEST", str(tmp_path / "missing.json"))
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "no-workspace"))
        monkeypatch.chdir(tmp_path)  # Prevent cwd fallback finding real manifest
        services = list_services()
        assert services == {}


class TestOllamaUrlOverrideName:
    """`ROBOTHOR_OLLAMA_URL` is the platform's name for the Ollama endpoint.

    The service registry read a bare `OLLAMA_URL`, which is Ollama's own
    variable and belongs to whatever else on the box speaks to it -- so an
    operator who set the documented `ROBOTHOR_OLLAMA_URL` got the manifest
    default and no indication why.
    """

    def test_canonical_name_overrides_the_manifest(self, monkeypatch):
        from robothor.services import registry

        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://gpu-box:11434")
        assert registry.get_service_url("ollama") == "http://gpu-box:11434"

    def test_deprecated_name_still_works(self, monkeypatch):
        from robothor.services import registry

        monkeypatch.delenv("ROBOTHOR_OLLAMA_URL", raising=False)
        monkeypatch.setenv("OLLAMA_URL", "http://old-box:11434")
        assert registry.get_service_url("ollama") == "http://old-box:11434"

    def test_canonical_name_wins_when_both_are_set(self, monkeypatch):
        from robothor.services import registry

        monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://gpu-box:11434")
        monkeypatch.setenv("OLLAMA_URL", "http://old-box:11434")
        assert registry.get_service_url("ollama") == "http://gpu-box:11434"

    def test_the_url_is_read_through_the_settings_layer(self, monkeypatch, tmp_path):
        """config.yaml configures the endpoint, not just the environment.

        The registry kept its own precedence tuple over `os.environ`, so the
        `settings:` block -- which is a supported source for every other
        setting, and the one `genus config set` writes -- did nothing here.
        An operator who set the endpoint with `genus config set` got the
        manifest default and no indication why.
        """
        from robothor.services import registry
        from robothor.settings import reset_settings

        config = tmp_path / ".robothor" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("settings:\n  ollama:\n    url: http://file-box:11434\n")
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ROBOTHOR_OLLAMA_URL", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        reset_settings()
        assert registry.get_service_url("ollama") == "http://file-box:11434"
