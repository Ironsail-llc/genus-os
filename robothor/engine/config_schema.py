"""Config validation — the legacy entry point, now a shim.

Every rule that used to live here moved to :mod:`robothor.engine.manifest_schema`,
which derives its structural half from ``robothor/engine/schema/agent_manifest.yaml``
instead of restating it by hand. The messages moved verbatim: they are grepped
by other tests and read by operators, so rewording one is a silent change to a
signal somebody may already be filtering on.

This function keeps its signature — ``dict -> list[str]`` — because a dozen
call sites and ``validate_agents.py --ci`` depend on it. Fail-open, as before:
it returns strings and never raises. Callers that want a manifest to be
REFUSED use ``manifest_schema.raise_if_invalid`` or the ``enforce`` rung of
``ROBOTHOR_MANIFEST_SCHEMA_MODE``.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine.manifest_schema import (
    _KNOWN_DIFFICULTY_CLASSES,
    _KNOWN_GUARDRAILS,
    _KNOWN_SANDBOX_MODES,
    _KNOWN_SESSION_TARGETS,
    _KNOWN_V2_KEYS,
    legacy_warnings,
    validate,
)

logger = logging.getLogger(__name__)

#: Re-exported, not re-derived. Two suites assert these agree with the sets
#: guardrails.py and config.py actually enforce, and the whole point of the
#: move is that there is now exactly ONE of each. An alias keeps those tests
#: pointed at a real single source instead of quietly passing against a copy.
__all__ = [
    "_KNOWN_DIFFICULTY_CLASSES",
    "_KNOWN_GUARDRAILS",
    "_KNOWN_SANDBOX_MODES",
    "_KNOWN_SESSION_TARGETS",
    "_KNOWN_V2_KEYS",
    "validate_manifest",
]


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Validate a merged manifest dict. Returns list of warning strings (empty = valid).

    Structural errors and every semantic finding come through. Non-error
    ``unknown_key`` findings deliberately do NOT — see
    :func:`~robothor.engine.manifest_schema.legacy_warnings` for why.
    """
    return legacy_warnings(validate(data))
