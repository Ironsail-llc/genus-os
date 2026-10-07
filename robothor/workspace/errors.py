"""The error vocabulary every workspace transport speaks.

A tool handler above the transport decides what to tell the agent from the
error TYPE, never from parsing a message, so each case that changes what the
caller may safely do next has its own class:

* :class:`UnknownEffect` -- a write left this process and no definite answer
  came back. It may have committed. It is NEVER retried by the transport; the
  caller must read back (or reconcile) before writing again, or the operator
  gets two copies of an email.
* :class:`RateLimited` -- the provider asked us to slow down; ``retry_after``
  says for how long when it said so.
* :class:`AuthError` -- credentials missing or refused. Its message carries at
  most the identity provider's error code (``invalid_client``) and the
  ``AADSTSnnnnn`` identifier, never the provider's free-text description,
  which quotes a rejected secret back verbatim.

Messages on every class are safe to show an operator: a provider error code
and message at most, never a token, a request body or a response body.
"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "NotFound",
    "PermissionDenied",
    "PreconditionFailed",
    "RateLimited",
    "UnknownEffect",
    "Unsupported",
    "WorkspaceError",
]


class WorkspaceError(RuntimeError):
    """Base class: a workspace operation failed. Carries no credential or body."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class NotFound(WorkspaceError):  # noqa: N818 - domain vocabulary, shared with PR 3
    """The message, event or mailbox does not exist (or is not visible to us)."""


class PreconditionFailed(WorkspaceError):  # noqa: N818
    """HTTP 412: the item changed since it was read (stale ETag/change key)."""


class PermissionDenied(WorkspaceError):  # noqa: N818
    """HTTP 401/403: the grant does not cover this mailbox or operation."""


class RateLimited(WorkspaceError):  # noqa: N818
    """The provider throttled us. ``retry_after`` is seconds, when it said."""

    def __init__(
        self,
        message: str = "workspace provider rate limit; retry later",
        *,
        retry_after: float | None = None,
        status: int | None = 429,
        code: str | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code)
        self.retry_after = retry_after


class UnknownEffect(WorkspaceError):  # noqa: N818
    """A write was sent and its outcome is unknown. Never retried automatically."""


class Unsupported(WorkspaceError):  # noqa: N818
    """The provider cannot express this request faithfully; refused, never widened."""

    def __init__(self, reason: str, *, status: int | None = None, code: str | None = None):
        super().__init__(reason, status=status, code=code)
        self.reason = reason


class AuthError(WorkspaceError):
    """Credentials are missing or were refused. Message carries a code, never a secret."""
