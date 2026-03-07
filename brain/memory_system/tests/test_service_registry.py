"""
Tests for service_registry.py — service manifest lookups, env overrides,
topological sort, and dependency resolution.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure memory_system is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from service_registry import (
    _reset_cache,
    get_dependencies,
    get_health_url,
    get_service,
    get_service_url,
    get_systemd_unit,
    list_services,
    topological_sort,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the manifest cache before each test."""
    _reset_cache()
    yield
    _reset_cache()


class TestManifestLoading:
    """Tests for manifest file loading and caching."""

    def test_loads_manifest_from_known_path(self):
        """The manifest file exists at the expected location."""
        services = list_services()
        assert isinstance(services, dict)
        assert len(services) > 0

    def test_manifest_has_bridge(self):
        svc = get_service("bridge")
        assert svc is not None
        assert svc["port"] == 9100

    def test_manifest_has_orchestrator(self):
        svc = get_service("orchestrator")
        assert svc is not None
        assert svc["port"] == 9099

    def test_manifest_has_vision(self):
        svc = get_service("vision")
        assert svc is not None
        assert svc["port"] == 8600

    def test_manifest_has_helm(self):
        svc = get_service("helm")
        assert svc is not None
        assert svc["port"] == 3004

    def test_manifest_has_gateway(self):
        svc = get_service("gateway")
        assert svc is not None
        assert svc["port"] == 18789

    def test_unknown_service_returns_none(self):
        assert get_service("nonexistent") is None


class TestGetServiceUrl:
    """Tests for URL generation from manifest."""

    def test_bridge_url(self):
        url = get_service_url("bridge")
        assert url == "http://127.0.0.1:9100"

    def test_orchestrator_url(self):
        url = get_service_url("orchestrator")
        assert url == "http://0.0.0.0:9099"

    def test_vision_url(self):
        url = get_service_url("vision")
        assert url == "http://0.0.0.0:8600"

    def test_gateway_url_is_websocket(self):
        url = get_service_url("gateway")
        assert url == "ws://0.0.0.0:18789"

    def test_url_with_path(self):
        url = get_service_url("bridge", "/api/people")
        assert url == "http://127.0.0.1:9100/api/people"

    def test_url_with_health_path(self):
        url = get_service_url("bridge", "/health")
        assert url == "http://127.0.0.1:9100/health"

    def test_unknown_service_returns_none(self):
        assert get_service_url("nonexistent") is None

    def test_env_override_takes_precedence(self):
        with patch.dict(os.environ, {"BRIDGE_URL": "http://custom:9999"}):
            url = get_service_url("bridge")
            assert url == "http://custom:9999"

    def test_env_override_with_path(self):
        with patch.dict(os.environ, {"BRIDGE_URL": "http://custom:9999"}):
            url = get_service_url("bridge", "/api/people")
            assert url == "http://custom:9999/api/people"

    def test_env_override_strips_trailing_slash(self):
        with patch.dict(os.environ, {"BRIDGE_URL": "http://custom:9999/"}):
            url = get_service_url("bridge", "/health")
            assert url == "http://custom:9999/health"

    def test_ollama_env_override(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://gpu-server:11434"}):
            url = get_service_url("ollama")
            assert url == "http://gpu-server:11434"


class TestGetHealthUrl:
    """Tests for health endpoint URL generation."""

    def test_bridge_health_url(self):
        url = get_health_url("bridge")
        assert url == "http://127.0.0.1:9100/health"

    def test_orchestrator_health_url(self):
        url = get_health_url("orchestrator")
        assert url == "http://0.0.0.0:9099/health"

    def test_service_without_health_returns_none(self):
        url = get_health_url("gateway")
        assert url is None

    def test_unknown_service_returns_none(self):
        assert get_health_url("nonexistent") is None


class TestDependencies:
    """Tests for dependency resolution."""

    def test_bridge_depends_on_postgres_and_redis(self):
        deps = get_dependencies("bridge")
        assert "postgres" in deps
        assert "redis" in deps

    def test_orchestrator_depends_on_postgres_redis_ollama(self):
        deps = get_dependencies("orchestrator")
        assert "postgres" in deps
        assert "redis" in deps
        assert "ollama" in deps

    def test_helm_depends_on_bridge_and_orchestrator(self):
        deps = get_dependencies("helm")
        assert "bridge" in deps
        assert "orchestrator" in deps

    def test_leaf_service_has_no_deps(self):
        deps = get_dependencies("redis")
        assert deps == []

    def test_unknown_service_returns_empty(self):
        assert get_dependencies("nonexistent") == []


class TestSystemdUnit:
    """Tests for systemd unit lookup."""

    def test_bridge_systemd_unit(self):
        assert get_systemd_unit("bridge") == "robothor-bridge.service"

    def test_helm_systemd_unit(self):
        assert get_systemd_unit("helm") == "robothor-app.service"

    def test_docker_service_has_no_unit(self):
        # Docker-managed services (kokoro) don't have systemd units
        assert get_systemd_unit("kokoro_tts") is None

    def test_unknown_service_returns_none(self):
        assert get_systemd_unit("nonexistent") is None


class TestTopologicalSort:
    """Tests for boot order resolution."""

    def test_returns_all_services(self):
        order = topological_sort()
        services = list_services()
        assert set(order) == set(services.keys())

    def test_postgres_before_bridge(self):
        order = topological_sort()
        assert order.index("postgres") < order.index("bridge")

    def test_redis_before_bridge(self):
        order = topological_sort()
        assert order.index("redis") < order.index("bridge")

    def test_bridge_before_helm(self):
        order = topological_sort()
        assert order.index("bridge") < order.index("helm")

    def test_orchestrator_before_helm(self):
        order = topological_sort()
        assert order.index("orchestrator") < order.index("helm")

    def test_ollama_before_orchestrator(self):
        order = topological_sort()
        assert order.index("ollama") < order.index("orchestrator")

    def test_mediamtx_before_vision(self):
        order = topological_sort()
        assert order.index("mediamtx") < order.index("vision")


class TestManifestSchema:
    """Tests validating the manifest structure."""

    def test_no_port_conflicts(self):
        """No two services share the same port."""
        services = list_services()
        ports = {}
        for name, svc in services.items():
            port = svc.get("port")
            if port in ports:
                pytest.fail(f"Port {port} conflict between '{ports[port]}' and '{name}'")
            ports[port] = name

    def test_all_services_have_port(self):
        services = list_services()
        for name, svc in services.items():
            assert "port" in svc, f"Service '{name}' missing 'port'"

    def test_dependency_graph_is_acyclic(self):
        """The topological sort must not raise ValueError."""
        # If this raises, there's a cycle
        order = topological_sort()
        assert len(order) > 0

    def test_dependencies_reference_known_services(self):
        """All dependency names reference services in the manifest."""
        services = list_services()
        for name, svc in services.items():
            for dep in svc.get("dependencies", []):
                assert dep in services, f"Service '{name}' depends on unknown service '{dep}'"

    def test_systemd_services_have_health(self):
        """Services with systemd units should have health endpoints."""
        services = list_services()
        # Services without HTTP health: WebSocket, RTSP, TCP-only, raw sockets
        exceptions = {"voice", "sms", "mediamtx", "gateway", "redis", "postgres"}
        for name, svc in services.items():
            if svc.get("systemd_unit") and name not in exceptions:
                assert svc.get("health") is not None, (
                    f"Service '{name}' has systemd unit but no health endpoint"
                )


class TestCyclicDependencyDetection:
    """Test cycle detection in dependency graph."""

    def test_detects_cycle(self, tmp_path):
        """Circular deps raise ValueError."""
        manifest = {
            "services": {
                "a": {"port": 1000, "dependencies": ["b"]},
                "b": {"port": 1001, "dependencies": ["a"]},
            }
        }
        manifest_path = tmp_path / "test-services.json"
        manifest_path.write_text(json.dumps(manifest))

        # Patch the manifest paths to use our test file
        import service_registry

        orig_paths = service_registry._MANIFEST_PATHS
        service_registry._MANIFEST_PATHS = [manifest_path]
        _reset_cache()

        try:
            with pytest.raises(ValueError, match="Circular dependency"):
                topological_sort()
        finally:
            service_registry._MANIFEST_PATHS = orig_paths
            _reset_cache()
