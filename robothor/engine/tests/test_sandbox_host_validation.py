"""`sandbox: host` must validate — the runner already honors it.

`_resolve_sandbox_decision` treats "host" as the explicit opt-out from
sandbox-by-default, and docs/agents/schema.yaml documents it, but the config
validator's mode set still only knew {local, docker}. A manifest using the
supported escape hatch therefore emitted "Unknown sandbox mode: 'host'" —
noise in the run log, and a hard failure under `validate_agents.py --ci`.
"""

from __future__ import annotations

from robothor.engine.config_schema import validate_manifest


def _manifest(sandbox: str) -> dict:
    return {
        "id": "a",
        "name": "A",
        "description": "d",
        "model": {"primary": "openrouter/x/y"},
        "v2": {"sandbox": sandbox},
    }


def test_host_is_a_valid_sandbox_mode():
    warnings = validate_manifest(_manifest("host"))
    assert not [w for w in warnings if "sandbox" in w.lower()], (
        f"`sandbox: host` should validate — the runner honors it. Got: {warnings}"
    )


def test_local_and_docker_still_valid():
    for mode in ("local", "docker"):
        warnings = validate_manifest(_manifest(mode))
        assert not [w for w in warnings if "sandbox" in w.lower()]


def test_unknown_mode_still_warns():
    warnings = validate_manifest(_manifest("nonsense"))
    assert any("Unknown sandbox mode" in w for w in warnings)
