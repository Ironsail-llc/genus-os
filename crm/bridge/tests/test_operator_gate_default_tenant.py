"""The Helm operator gate's tenant fallback must agree with the platform default.

``PLATFORM_TENANT`` used to bottom out on a hardcoded first-instance tenant id
('robothor-primary') while the bridge's own tenant middleware bottomed out on
``robothor.constants.DEFAULT_TENANT`` ('default').  On a fresh install with
neither env var set, sessions resolved tenant 'default' but the gate demanded
'robothor-primary' — every Helm operator route returned 403 out of the box.

These tests pin the contract: the gate's fallback chain is
``ROBOTHOR_PLATFORM_TENANT`` env, then ``DEFAULT_TENANT`` (which itself honors
``ROBOTHOR_DEFAULT_TENANT``).  No literal tenant id anywhere.
"""

from __future__ import annotations

import importlib

import routers._operator as operator_module

import robothor.constants


def _reload_with_env(monkeypatch, **env: str | None):
    """Re-execute the modules under a controlled tenant environment."""
    for name in ("ROBOTHOR_PLATFORM_TENANT", "ROBOTHOR_DEFAULT_TENANT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    importlib.reload(robothor.constants)
    importlib.reload(operator_module)


def _restore(monkeypatch):
    """Put the real environment back and re-derive module-level values."""
    monkeypatch.undo()
    importlib.reload(robothor.constants)
    importlib.reload(operator_module)


def test_fresh_install_fallback_is_default_tenant(monkeypatch):
    """Neither env var set: the gate and the middleware agree on DEFAULT_TENANT."""
    try:
        _reload_with_env(monkeypatch)
        assert operator_module.PLATFORM_TENANT == robothor.constants.DEFAULT_TENANT
        assert operator_module.PLATFORM_TENANT == "default"
    finally:
        _restore(monkeypatch)


def test_platform_tenant_env_wins(monkeypatch):
    try:
        _reload_with_env(
            monkeypatch,
            ROBOTHOR_PLATFORM_TENANT="acme-platform",
            ROBOTHOR_DEFAULT_TENANT="acme-default",
        )
        assert operator_module.PLATFORM_TENANT == "acme-platform"
    finally:
        _restore(monkeypatch)


def test_default_tenant_env_flows_through(monkeypatch):
    try:
        _reload_with_env(monkeypatch, ROBOTHOR_DEFAULT_TENANT="acme-default")
        assert operator_module.PLATFORM_TENANT == "acme-default"
    finally:
        _restore(monkeypatch)


def test_no_hardcoded_instance_tenant_in_source():
    """CLAUDE.md rule 1: no hardcoded tenant IDs in platform code."""
    import inspect

    source = inspect.getsource(operator_module)
    assert "robothor-primary" not in source
