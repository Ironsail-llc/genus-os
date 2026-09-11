"""A 422 must not hand the credential back.

FastAPI's default validation handler reflects the offending request body into
the response as ``input``, which is exactly the right behaviour everywhere
except on the handful of routes whose body IS a secret. On those, a mistyped
field name — ``{"apikey": "sk-…"}`` instead of ``{"api_key": …}`` — bounces the
operator's provider key straight back out of the appliance and into anything
that records 4xx bodies.

The redaction is scoped to those paths rather than applied globally: a 422 on
an ordinary route is a debugging aid and stripping it would make every other
router harder to work on to fix a problem none of them have.

Shared by the engine and the bridge because both terminate the same routes —
the bridge's ``PUT /api/providers/{id}/keys/{n}`` and the engine's
``POST /api/admin/providers/{id}/test`` each receive a key in a request body,
and a second copy of this rule would be one drift away from covering only one
of them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

#: Every route whose request body may carry key material. Prefix-anchored so a
#: route added under one of them inherits the protection instead of having to
#: remember it.
CREDENTIAL_BODY_PATHS: tuple[re.Pattern[str], ...] = (
    # The whole engine admin surface, not just the two routes that take a key
    # today: the same reasoning as its entry in the engine's control paths —
    # a route added under this prefix should inherit the rule rather than have
    # to remember it, and losing `input` on a credential-adjacent GET costs
    # nothing.
    re.compile(r"^/api/admin(?:/|$)"),
    re.compile(r"^/api/providers(?:/|$)"),
    re.compile(r"^/api/vault(?:/|$)"),
)

#: Validation-error members that can hold a fragment of the request.
_ECHOING_FIELDS = frozenset({"input", "ctx", "url"})


def carries_credentials(path: str) -> bool:
    """Whether a request to this path may have a secret in its body."""
    return any(pattern.match(path) for pattern in CREDENTIAL_BODY_PATHS)


def redact_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the type, the location and the message; drop the echoed value.

    The operator still learns *which field* was wrong and *why*, which is all
    a form needs to point at the offending input it already has on screen.
    """
    return [{k: v for k, v in error.items() if k not in _ECHOING_FIELDS} for error in errors]


def install_credential_safe_validation(app: Any) -> None:
    """Replace the default 422 handler with one that redacts on secret routes."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        errors = list(exc.errors()) if isinstance(exc, RequestValidationError) else []
        if carries_credentials(request.url.path):
            errors = redact_validation_errors(errors)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    app.add_exception_handler(RequestValidationError, _handler)
