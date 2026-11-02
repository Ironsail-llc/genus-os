"""One shape for every refusal the engine hands a model.

A refusal that says only what happened ("Tool 'x' is not available") leaves the
model two moves: tell the person it cannot, or look for a way round. Both are
wrong when a person could unlock the call. :func:`refusal` adds what the person
needs:

``refused``    ``True``: a control declined the call; the tool did not fail.
``gate``       which KIND of control: one of :data:`GATES`.
``gate_name``  the specific control (``plan_mode``, ``rbac``, ``GITHUB_TOKEN``).
``unlock``     what a person changes for the same call to succeed.

``error`` is always the existing text, and any legacy keys a caller already
reads (``denied_by``, ``error_type``, ``guard``) are kept.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GATES", "refusal", "refusal_fields"]

#: ``role``      the person's or the run's role does not allow it (RBAC, owner-only).
#: ``flag``      a feature flag or setting switch is off.
#: ``allowlist`` the agent's manifest, or a parent's whitelist, does not grant the tool.
#: ``runtime``   something the instance lacks: a credential, a socket, a binary, a path.
#: ``policy``    a control that is working as designed for this run (plan mode, a
#:               guardrail, a hook, the wrap-up window).
GATES = frozenset({"role", "flag", "allowlist", "runtime", "policy"})


def refusal_fields(*, gate: str, name: str, unlock: str) -> dict[str, Any]:
    """The four refusal keys. Raises on an unknown gate."""
    if gate not in GATES:
        raise ValueError(f"unknown refusal gate {gate!r}; expected one of {sorted(GATES)}")
    return {"refused": True, "gate": gate, "gate_name": name, "unlock": unlock}


def refusal(message: str, *, gate: str, name: str, unlock: str, **legacy: Any) -> dict[str, Any]:
    """A refusal payload: ``error``, any ``legacy`` keys, and the four refusal keys."""
    return {"error": message, **legacy, **refusal_fields(gate=gate, name=name, unlock=unlock)}
