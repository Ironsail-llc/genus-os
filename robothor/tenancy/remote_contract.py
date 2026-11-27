"""Shared names for remote tools: a parent tenant's agent running a descendant's tool.

The home engine, the bridge and the target engine all import these rather than
retyping strings, so a refusal code or a limit cannot drift between the hops.
Pure: no settings, no database, nothing heavier than the standard library.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# ── Access, outcomes ─────────────────────────────────────────────────────────

ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_VALUES: frozenset[str] = frozenset({ACCESS_READ, ACCESS_WRITE})

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_DRY_RUN = "dry_run"
OUTCOME_REPLAYED = "replayed"
OUTCOMES: frozenset[str] = frozenset({OUTCOME_OK, OUTCOME_ERROR, OUTCOME_DRY_RUN, OUTCOME_REPLAYED})

# ── Refusal codes ────────────────────────────────────────────────────────────

ERR_WRITE_FORBIDDEN = "remote_write_forbidden"
ERR_TOOL_UNKNOWN = "remote_tool_unknown"
ERR_ACCESS_MISMATCH = "remote_access_mismatch"
ERR_CALLER_FORBIDDEN = "remote_caller_forbidden"
ERR_NOT_PERMITTED = "remote_not_permitted"
ERR_ATTESTATION_MISSING = "remote_attestation_missing"
ERR_BAD_ARGUMENTS = "remote_bad_arguments"
ERR_OUTCOME_UNKNOWN = "remote_outcome_unknown"
ERR_TOO_LARGE = "remote_too_large"
ERR_TIMEOUT = "remote_timeout"

ERROR_CODES: frozenset[str] = frozenset(
    {
        ERR_WRITE_FORBIDDEN,
        ERR_TOOL_UNKNOWN,
        ERR_ACCESS_MISMATCH,
        ERR_CALLER_FORBIDDEN,
        ERR_NOT_PERMITTED,
        ERR_ATTESTATION_MISSING,
        ERR_BAD_ARGUMENTS,
        ERR_OUTCOME_UNKNOWN,
        ERR_TOO_LARGE,
        ERR_TIMEOUT,
    }
)

# ── Names ────────────────────────────────────────────────────────────────────

#: Audit event written under the TARGET tenant for every remote call.
EVENT_REMOTE_TOOL_CALL = "agent.remote_tool_call"
#: Role a target tenant grants, per tool, for person-less reads from its parent.
ROLE_PARENT_AGENT = "parent_agent"
#: Service-token scope an engine needs to call the bridge's remote routes.
SCOPE_ORG_REMOTE = "org:remote"
#: The only service id a target engine accepts remote calls from.
BRIDGE_SERVICE_ID = "genus-bridge"

#: The bridge routes a remote call uses, as ``bridge_endpoints`` patterns, each
#: with one request it must match. An agent the bridge's capabilities manifest
#: LISTS needs both; an unlisted one is authorised by :data:`SCOPE_ORG_REMOTE`.
ORG_REMOTE_ENDPOINT_SAMPLES: tuple[tuple[str, str, str], ...] = (
    ("GET /api/org/remote/*", "GET", "/api/org/remote/catalogue"),
    ("POST /api/org/remote/call", "POST", "/api/org/remote/call"),
)
ORG_REMOTE_ENDPOINTS: tuple[str, ...] = tuple(p for p, _, _ in ORG_REMOTE_ENDPOINT_SAMPLES)

#: A plugin's attestation token (and a deployment's attested one).
ATTESTATION_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# ── Limits ───────────────────────────────────────────────────────────────────

REMOTE_RESULT_MAX_BYTES = 262144  # a larger result is refused, never truncated
TARGET_TIMEOUT_S = 150  # the target engine's own execution budget
BRIDGE_TIMEOUT_S = 165  # bridge -> target
HOME_TIMEOUT_S = 180  # home engine -> bridge
CHECK_CACHE_TTL_S = 60  # catalogue doctor-check cache
DESCRIPTION_MAX_CHARS = 200  # catalogue description trim
NOTES_MAX_CHARS = 600  # a declaration's notes


def args_digest(args: Any) -> str:
    """sha256 hex of the arguments as canonical JSON (sorted keys, no whitespace).

    Audit rows carry this instead of the arguments. ``None`` digests as ``{}``.
    """
    canonical = json.dumps(
        {} if args is None else args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
